"""Idempotent, checkpointed data ingestion with conservative publication times."""
from __future__ import annotations

import calendar
import json
import zlib
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .sources import FinMind, HttpClient, RateLimitError, SourceError, iso_date, number, official_snapshot
from .store import Store

LOG = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Taipei")

DATASETS = {
    "prices": "TaiwanStockPrice",
    "revenue": "TaiwanStockMonthRevenue",
    "financials": "TaiwanStockFinancialStatements",
    "balance": "TaiwanStockBalanceSheet",
    "institutions": "TaiwanStockInstitutionalInvestorsBuySell",
    "margin": "TaiwanStockMarginPurchaseShortSale",
    "actions": "TaiwanStockDividendResult",
}


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _required(row: dict, keys: tuple[str, ...]) -> None:
    missing = [x for x in keys if x not in row]
    if missing:
        raise SourceError(f"Provider schema changed; missing: {missing}")


def _code(row: dict, expected: str) -> str:
    code = str(row.get("stock_id", expected)).strip()
    if code != expected:
        raise SourceError(f"Cross-stock response: {expected} vs {code}")
    return code


def ingest_dataset(store: Store, dataset: str, code: str, data: list[dict],
                   observed: str | None = None) -> int:
    """Parse and validate a whole response before writing any of its rows."""
    t = observed or now()
    result: list[tuple] = []
    table, cols = "", ()
    for row in data:
        _code(row, code)
        if dataset == "prices":
            _required(row, ("date", "open", "max", "min", "close", "Trading_Volume"))
            d = iso_date(row["date"])
            o, h, l, c = [number(row[k]) for k in ("open", "max", "min", "close")]
            v = number(row["Trading_Volume"], integer=True)
            if v is None or v < 0:
                raise SourceError("Invalid volume")
            # FinMind may report zero prices on an otherwise valid no-price day.
            if all(x == 0 for x in (o, h, l, c)):
                o = h = l = c = None
            elif None in (o, h, l, c) or min(o, h, l, c) <= 0 or not l <= o <= h or not l <= c <= h:
                raise SourceError(f"Invalid OHLC {code} {d}: {o,h,l,c}")
            table = "prices"
            cols = ("code", "trade_date", "open", "high", "low", "close", "volume_shares",
                    "turnover_twd", "source", "published_at", "observed_at", "availability")
            result.append((code, d, o, h, l, c, v, number(row.get("Trading_money")),
                           "FinMind/TaiwanStockPrice", None, t, "historical_date_unverified"))
        elif dataset == "revenue":
            _required(row, ("revenue_year", "revenue_month", "revenue"))
            year, month = int(row["revenue_year"]), int(row["revenue_month"])
            period = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
            provider_seen = str(row.get("create_time") or "")[:10] or None
            if provider_seen:
                provider_seen = iso_date(provider_seen)
            value = number(row["revenue"])
            if value is None:
                raise SourceError("Missing revenue")
            table = "monthly_revenue"
            cols = ("code", "period_end", "revenue_twd", "published_at",
                    "provider_observed_on", "observed_at", "source")
            result.append((code, period, value, None, provider_seen, t,
                           "FinMind/TaiwanStockMonthRevenue"))
        elif dataset in ("financials", "balance"):
            _required(row, ("date", "type", "value"))
            value = number(row["value"])
            if value is None:
                continue
            metric = str(row["type"])
            if not metric or len(metric) > 150:
                raise SourceError("Invalid statement metric")
            table = "quarterly_financials"
            cols = ("code", "period_end", "metric", "value", "original_name",
                    "published_at", "observed_at", "source")
            result.append((code, iso_date(row["date"]),
                           ("bs:" if dataset == "balance" else "is:") + metric,
                           value, row.get("origin_name"), None, t,
                           "FinMind/" + DATASETS[dataset]))
        elif dataset == "institutions":
            _required(row, ("date", "name", "buy", "sell"))
            buy = number(row["buy"], integer=True)
            sell = number(row["sell"], integer=True)
            if buy is None or sell is None or min(buy, sell) < 0:
                raise SourceError("Invalid institutional volumes")
            table = "institutional_flows"
            cols = ("code", "trade_date", "institution", "buy_shares", "sell_shares",
                    "published_at", "observed_at", "source")
            result.append((code, iso_date(row["date"]), str(row["name"]), buy, sell,
                           None, t, "FinMind/TaiwanStockInstitutionalInvestorsBuySell"))
        elif dataset == "margin":
            _required(row, ("date", "MarginPurchaseTodayBalance", "ShortSaleTodayBalance"))
            table = "margin_balances"
            cols = ("code", "trade_date", "margin_balance_lots", "short_balance_lots",
                    "payload", "published_at", "observed_at", "source")
            result.append((code, iso_date(row["date"]),
                           number(row["MarginPurchaseTodayBalance"], integer=True),
                           number(row["ShortSaleTodayBalance"], integer=True),
                           json.dumps(row, ensure_ascii=False, sort_keys=True), None, t,
                           "FinMind/TaiwanStockMarginPurchaseShortSale"))
        elif dataset == "actions":
            _required(row, ("date", "before_price", "after_price"))
            before, after = number(row["before_price"]), number(row["after_price"])
            if before is None or after is None or min(before, after) <= 0:
                raise SourceError("Invalid action prices")
            table = "corporate_actions"
            cols = ("code", "ex_date", "before_price", "after_price", "action_value",
                    "action_kind", "published_at", "observed_at", "source")
            result.append((code, iso_date(row["date"]), before, after,
                           number(row.get("stock_and_cache_dividend")),
                           str(row.get("stock_or_cache_dividend", "")), None, t,
                           "FinMind/TaiwanStockDividendResult"))
        else:
            raise ValueError(f"Unsupported dataset: {dataset}")
    if not result:
        return 0
    return store.upsert(table, cols, result)


def ingest_stock_info(store: Store, rows: list[dict], observed: str | None = None) -> int:
    t = observed or now()
    history = []
    latest = {}
    for row in rows:
        _required(row, ("stock_id", "stock_name", "type", "date"))
        code, market = str(row["stock_id"]), str(row["type"])
        if market not in ("twse", "tpex") or len(code) != 4 or not code.isdigit():
            continue
        d = iso_date(row["date"])
        name = str(row["stock_name"])
        history.append((code, market, d, name, t))
        if code not in latest or d > latest[code][3]:
            latest[code] = (code, name, market, d, row.get("industry_category"), t,
                            "FinMind/TaiwanStockInfo")
    store.upsert("security_history", ("code", "market", "source_date", "name", "observed_at"), history)
    store.upsert("securities", ("code", "name", "market", "info_date", "industry",
                                "last_seen_at", "source"), latest.values())
    return len(latest)


def ingest_delistings(store: Store, rows: list[dict], observed: str | None = None) -> int:
    """Preserve unverified provider columns so the exact delisting date can be audited."""
    t = observed or now()
    parsed = []
    for r in rows:
        _required(r, ("stock_id", "date"))  # date's meaning: 需驗證
        code = str(r["stock_id"])
        if len(code) == 4 and code.isdigit():
            parsed.append((code, iso_date(r["date"]),
                           json.dumps(r, ensure_ascii=False, sort_keys=True), t,
                           "FinMind/TaiwanStockDelisting"))
    return store.upsert("delistings", ("code", "event_date", "payload", "observed_at", "source"), parsed)


def adjusted_for_stock(store: Store, code: str, t: str | None = None) -> int:
    """Partial ex-post back adjustment. NEVER use this for filling orders or PIT signals."""
    prices = store.db.execute("SELECT trade_date,close FROM prices WHERE code=? ORDER BY trade_date DESC", (code,)).fetchall()
    events = store.db.execute("SELECT ex_date,before_price,after_price FROM corporate_actions "
                              "WHERE code=? ORDER BY ex_date DESC", (code,)).fetchall()
    if not prices or not events:
        return 0  # Incomplete action coverage must not be presented as adjusted data.
    i, factor, out = 0, 1.0, []
    for p in prices:
        d, close = p
        # The event's adjustment applies to dates STRICTLY before ex-date.
        while i < len(events) and events[i][0] > d:
            factor *= events[i][2] / events[i][1]
            i += 1
        if close is not None:
            out.append((code, d, close * factor, factor,
                        "partial_expost_dividend_result_factor", t or now()))
    return store.upsert("price_adjusted", ("code", "trade_date", "adjusted_close",
                                           "adjustment_factor", "method", "calculated_at"), out)


def ingest_official(store: Store, http: HttpClient, market: str) -> int:
    """Latest official snapshot only; reject missing/ambiguous dates."""
    rows = official_snapshot(http, market)
    t = now()
    values = []
    for r in rows:
        if None in (r["open"], r["high"], r["low"], r["close"]):
            continue
        if not r["low"] <= min(r["open"], r["close"]) <= max(r["open"], r["close"]) <= r["high"]:
            raise SourceError(f"Official invalid OHLC {r['code']} {r['date']}")
        values.append((r["code"], r["date"], r["open"], r["high"], r["low"],
                       r["close"], r["volume"], r["turnover"], f"{market.upper()}/OpenAPI",
                       t[:10], t, "observed_after_close"))
    store.upsert("prices", ("code", "trade_date", "open", "high", "low", "close",
                            "volume_shares", "turnover_twd", "source", "published_at",
                            "observed_at", "availability"), values)
    store.upsert("securities", ("code", "name", "market", "info_date", "industry",
                                "last_seen_at", "source"),
                 [(r["code"], str(r["name"]), market, r["date"], None, t,
                   f"{market.upper()}/OpenAPI") for r in rows])
    return len(values)


# Swagger lists these feeds. Their *row schema and historic query parameters* are 需驗證.
# For that reason store the untouched source response and never infer row fields.
OFFICIAL_FEEDS = {
    "twse_margin": "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN",
    "tpex_margin": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance",
    "tpex_institutions": "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading",
    "twse_revenue": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    "tpex_revenue": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
    "twse_income_general": "https://openapi.twse.com.tw/v1/opendata/t187ap06_X_ci",
    "twse_balance_general": "https://openapi.twse.com.tw/v1/opendata/t187ap07_X_ci",
    "tpex_income_general": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci",
    "tpex_balance_general": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_ci",
}


def archive_official_reports(store: Store, http: HttpClient, feeds: list[str]) -> dict[str, int]:
    counts = {}
    for name in feeds:
        if name not in OFFICIAL_FEEDS:
            raise ValueError(f"Unknown feed {name}")
        payload = http.get_json(OFFICIAL_FEEDS[name])
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise SourceError(f"Official feed {name} requires response/schema verification")
        snapshot = zlib.compress(json.dumps(payload, ensure_ascii=False,
                                              separators=(",", ":")).encode("utf-8"), level=6)
        stamp = now()
        counts[name] = store.upsert("official_reports",
            ("feed", "retrieved_on", "payload_blob", "observed_at", "published_at"),
            [(name, stamp[:10], snapshot, stamp, None)])
    return counts


def _start_for(store: Store, dataset: str, code: str, initial: date, end: date) -> date:
    previous = store.checkpoint(dataset, code)
    overlap = {"revenue": 90, "financials": 180, "balance": 180,
               "actions": 90, "prices": 10, "institutions": 10, "margin": 10}[dataset]
    return max(initial, date.fromisoformat(previous) - timedelta(days=overlap)) if previous else initial


def sync(store: Store, api: FinMind, codes: list[str], datasets: list[str],
         start: date, end: date, *, max_requests: int = 250) -> dict[str, int]:
    if max_requests < 1:
        raise ValueError("max_requests must be positive")
    counts = {name: 0 for name in datasets}
    requests = 0
    jobs = [(code, dataset) for code in codes for dataset in datasets]
    # Each run works on the least recently synced jobs, so a daily quota cannot
    # keep restarting from 1101 and starve the rest of the market forever.
    states = {(r[0], r[1]): r[2] for r in store.db.execute(
        "SELECT code,dataset,updated_at FROM sync_state")}
    jobs.sort(key=lambda item: (states.get(item, ""), item[0], item[1]))
    for code, dataset in jobs:
        if store.checkpoint(dataset, code) == end.isoformat():
            continue
        if requests >= max_requests:
            LOG.warning("Request budget used; rerun the same command to resume")
            return counts
        begin = _start_for(store, dataset, code, start, end)
        if begin > end:
            continue
        requests += 1
        try:
            rows = api.fetch(DATASETS[dataset], code, begin.isoformat(), end.isoformat())
            counts[dataset] += ingest_dataset(store, dataset, code, rows)
            store.mark_success(dataset, code, end.isoformat(), now())
            if dataset in ("prices", "actions"):
                adjusted_for_stock(store, code)
            LOG.info("%s %s: %d records", code, dataset, len(rows))
        except RateLimitError as e:
            store.mark_error(dataset, code, now(), e)
            LOG.error("Quota reached after %s requests; resume next hour", requests)
            return counts
        except (SourceError, ValueError, KeyError) as e:
            store.mark_error(dataset, code, now(), e)
            LOG.error("%s %s: %s", code, dataset, e)
    return counts
