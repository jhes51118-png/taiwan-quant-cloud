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

OFFICIAL_REVENUE_FEEDS = {
    "twse": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    "tpex": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
}

OFFICIAL_MARGIN_FEEDS = {
    "twse": "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN",
    "tpex": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance",
}

OFFICIAL_TPEX_INSTITUTIONS = (
    "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
)

_STATEMENT_KINDS = ("basi", "bd", "ci", "fh", "ins", "mim")


def _official_rows(http: HttpClient, url: str, feed: str, *, allow_empty: bool = False) -> list[dict]:
    rows = http.get_json(url)
    if not isinstance(rows, list) or (not rows and not allow_empty):
        raise SourceError(f"Official feed {feed} returned an unexpected response")
    if any(not isinstance(row, dict) for row in rows):
        raise SourceError(f"Official feed {feed} contains a non-object row")
    return rows


def _ordinary_code(value: object) -> str | None:
    code = str(value).strip()
    return code if len(code) == 4 and code.isdigit() and not code.startswith("0") else None


def _roc_year(value: object) -> int:
    year = int(str(value).strip())
    return year + 1911 if year < 1911 else year


def _month_end(value: object) -> str:
    raw = str(value).strip()
    if not raw.isdigit() or len(raw) not in (5, 6):
        raise SourceError(f"Invalid official revenue period: {raw}")
    year, month = _roc_year(raw[:-2]), int(raw[-2:])
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def _quarter_end(year: object, quarter: object) -> str:
    y, q = _roc_year(year), int(str(quarter).strip())
    if q not in (1, 2, 3, 4):
        raise SourceError(f"Invalid official statement quarter: {quarter}")
    month = q * 3
    return date(y, month, calendar.monthrange(y, month)[1]).isoformat()


def _field(row: dict, candidates: tuple[str, ...], *, required: bool = True) -> tuple[object, str]:
    for key in candidates:
        if key in row:
            return row[key], key
    if required:
        raise SourceError(f"Official schema changed; missing one of {candidates}")
    return None, ""


def ingest_official_revenue(store: Store, http: HttpClient, market: str) -> int:
    """Save the latest official monthly revenue snapshot with its report date.

    The official monetary columns are NT$ thousands; storage normalizes them to
    TWD.  `出表日期` is later than or equal to the company filing time, so using
    it as published_at is conservative for point-in-time research.
    """
    if market not in OFFICIAL_REVENUE_FEEDS:
        raise ValueError("unknown market")
    rows = _official_rows(http, OFFICIAL_REVENUE_FEEDS[market], f"{market}_revenue")
    code_key = "公司代號" if market == "twse" else "公司代號"
    needed = {"出表日期", "資料年月", code_key, "營業收入-當月營收"}
    if not needed.issubset(rows[0]):
        raise SourceError(f"{market} revenue columns need verification: {sorted(needed-set(rows[0]))}")
    observed = now()
    values = []
    for row in rows:
        code = _ordinary_code(row.get(code_key))
        if not code:
            continue
        revenue = number(row.get("營業收入-當月營收"))
        # Investment companies can legitimately report negative revenue when
        # valuation losses exceed operating income (for example TPEx 7777).
        if revenue is None:
            raise SourceError(f"Invalid official revenue for {code}")
        published = iso_date(row["出表日期"])
        values.append((code, _month_end(row["資料年月"]), int(round(revenue * 1000)),
                       published, published, observed,
                       f"{market.upper()}/OpenAPI/monthly_revenue"))
    return store.upsert("monthly_revenue",
                        ("code", "period_end", "revenue_twd", "published_at",
                         "provider_observed_on", "observed_at", "source"), values)


def ingest_official_margin(store: Store, http: HttpClient, market: str,
                           *, latest_quote_date: str | None = None) -> int:
    """Save official financing/short balances.

    TPEx supplies an explicit report date.  TWSE's documented MI_MARGN schema
    does not; its date is therefore tied to the latest saved TWSE quote and the
    source label explicitly retains `date_inferred` (需驗證).
    """
    if market not in OFFICIAL_MARGIN_FEEDS:
        raise ValueError("unknown market")
    rows = _official_rows(http, OFFICIAL_MARGIN_FEEDS[market], f"{market}_margin")
    observed = now()
    if market == "twse":
        needed = {"股票代號", "融資今日餘額", "融券今日餘額"}
        code_key, margin_key, short_key = "股票代號", "融資今日餘額", "融券今日餘額"
        if not latest_quote_date:
            raise SourceError("TWSE margin date needs the latest verified quote date")
        source = "TWSE/OpenAPI/margin_balance/date_inferred_需驗證"
    else:
        needed = {"Date", "SecuritiesCompanyCode", "MarginPurchaseBalance", "ShortSaleBalance"}
        code_key, margin_key, short_key = ("SecuritiesCompanyCode", "MarginPurchaseBalance",
                                           "ShortSaleBalance")
        source = "TPEX/OpenAPI/margin_balance"
    if not needed.issubset(rows[0]):
        raise SourceError(f"{market} margin columns need verification: {sorted(needed-set(rows[0]))}")
    values = []
    for row in rows:
        code = _ordinary_code(row.get(code_key))
        if not code:
            continue
        trade_day = latest_quote_date if market == "twse" else iso_date(row["Date"])
        margin = number(row.get(margin_key), integer=True)
        short = number(row.get(short_key), integer=True)
        if margin is None and short is None:
            continue
        if any(value is not None and value < 0 for value in (margin, short)):
            raise SourceError(f"Invalid official margin balance for {code}")
        payload = dict(row)
        if market == "twse":
            payload["_date_basis"] = "latest verified TWSE quote date; 需驗證"
        values.append((code, trade_day, margin, short,
                       json.dumps(payload, ensure_ascii=False, sort_keys=True),
                       trade_day if market == "tpex" else observed[:10], observed, source))
    return store.upsert("margin_balances",
                        ("code", "trade_date", "margin_balance_lots", "short_balance_lots",
                         "payload", "published_at", "observed_at", "source"), values)


def ingest_official_tpex_institutions(store: Store, http: HttpClient) -> int:
    """Normalize the documented TPEx per-stock three-institution feed."""
    rows = _official_rows(http, OFFICIAL_TPEX_INSTITUTIONS, "tpex_institutions")
    pairs = {
        "foreign": ("ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy",
                    "ForeignInvestorsIncludeMainlandAreaInvestors-TotalSell"),
        "investment_trust": ("SecuritiesInvestmentTrustCompanies-TotalBuy",
                             "SecuritiesInvestmentTrustCompanies-TotalSell"),
        "dealer": ("Dealers-TotalBuy", "Dealers-TotalSell"),
    }
    needed = {"Date", "SecuritiesCompanyCode"} | {x for pair in pairs.values() for x in pair}
    if not needed.issubset(rows[0]):
        raise SourceError(f"TPEx institution columns need verification: {sorted(needed-set(rows[0]))}")
    observed, values = now(), []
    for row in rows:
        code = _ordinary_code(row.get("SecuritiesCompanyCode"))
        if not code:
            continue
        trade_day = iso_date(row["Date"])
        for institution, (buy_key, sell_key) in pairs.items():
            buy, sell = number(row[buy_key], integer=True), number(row[sell_key], integer=True)
            if buy is None or sell is None or min(buy, sell) < 0:
                raise SourceError(f"Invalid TPEx institutional volume for {code}")
            values.append((code, trade_day, institution, buy, sell, trade_day, observed,
                           "TPEX/OpenAPI/three_institutions"))
    return store.upsert("institutional_flows",
                        ("code", "trade_date", "institution", "buy_shares", "sell_shares",
                         "published_at", "observed_at", "source"), values)


def _statement_url(market: str, statement: str, kind: str) -> str:
    if market == "twse":
        return f"https://openapi.twse.com.tw/v1/opendata/t187ap{'06' if statement == 'income' else '07'}_L_{kind}"
    if market == "tpex":
        return f"https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap{'06' if statement == 'income' else '07'}_O_{kind}"
    raise ValueError("unknown market")


def _statement_metrics(row: dict, statement: str) -> list[tuple[str, float, str]]:
    if statement == "income":
        definitions = {
            "is:EPS": (("基本每股盈餘（元）",), 1.0),
            "is:Revenue": (("營業收入",), 1000.0),
            "is:GrossProfit": (("營業毛利（毛損）淨額", "營業毛利（毛損）"), 1000.0),
            "is:IncomeAfterTaxes": (("淨利（淨損）歸屬於母公司業主",
                                      "淨利（損）歸屬於母公司業主"), 1000.0),
        }
    else:
        definitions = {
            "bs:EquityAttributableToOwnersOfParent":
                (("歸屬於母公司業主之權益合計", "歸屬於母公司業主權益合計",
                  "歸屬於母公司業主之權益"), 1000.0),
            "bs:Assets": (("資產總計",), 1000.0),
            "bs:Liabilities": (("負債總計",), 1000.0),
        }
    metrics = []
    for metric, (candidates, multiplier) in definitions.items():
        raw, original = _field(row, candidates, required=False)
        value = number(raw)
        if value is not None:
            metrics.append((metric, value * multiplier, original))
    return metrics


def _derive_official_financial_metrics(store: Store, observed: str) -> int:
    rows = store.db.execute(
        "SELECT code,period_end,metric,value,published_at FROM quarterly_financials "
        "WHERE source LIKE '%/OpenAPI/financials/%'"
    ).fetchall()
    grouped: dict[tuple[str, str], dict[str, tuple[float, str]]] = {}
    for code, period, metric, value, published in rows:
        grouped.setdefault((code, period), {})[metric] = (value, published)
    values = []
    for (code, period), metrics in grouped.items():
        revenue, gross = metrics.get("is:Revenue"), metrics.get("is:GrossProfit")
        income, equity = metrics.get("is:IncomeAfterTaxes"), metrics.get(
            "bs:EquityAttributableToOwnersOfParent")
        if revenue and gross and revenue[0] != 0:
            published = max(revenue[1], gross[1])
            values.append((code, period, "is:GrossMargin", gross[0] / revenue[0],
                           "推估毛利率=累計營業毛利/累計營業收入", published, observed,
                           "DERIVED/OfficialOpenAPI"))
        if income and equity and equity[0] > 0:
            quarter = (date.fromisoformat(period).month // 3)
            published = max(income[1], equity[1])
            values.append((code, period, "is:ROE", income[0] / equity[0] * 4 / quarter,
                           "推估年化ROE=累計歸母淨利/期末歸母權益×4/季數", published, observed,
                           "DERIVED/OfficialOpenAPI"))
        eps = metrics.get("is:EPS")
        if eps:
            quarter = (date.fromisoformat(period).month // 3)
            values.append((code, period, "is:EPSAnnualized", eps[0] * 4 / quarter,
                           "推估年化EPS=累計EPS×4/季數", eps[1], observed,
                           "DERIVED/OfficialOpenAPI"))
    return store.upsert("quarterly_financials",
                        ("code", "period_end", "metric", "value", "original_name",
                         "published_at", "observed_at", "source"), values)


def ingest_official_financials(store: Store, http: HttpClient, market: str) -> int:
    """Save selected PIT-safe fields from every official statement industry schema."""
    if market not in ("twse", "tpex"):
        raise ValueError("unknown market")
    observed, total = now(), 0
    for statement in ("income", "balance"):
        for kind in _STATEMENT_KINDS:
            feed = f"{market}_{statement}_{kind}"
            rows = _official_rows(http, _statement_url(market, statement, kind), feed,
                                  allow_empty=True)
            if not rows:
                continue
            values = []
            for row in rows:
                code_raw, _ = _field(row, ("公司代號", "SecuritiesCompanyCode"))
                code = _ordinary_code(code_raw)
                if not code:
                    continue
                year, _ = _field(row, ("年度", "Year"))
                quarter, _ = _field(row, ("季別", "Season"))
                report_date, _ = _field(row, ("出表日期", "Date"))
                period, published = _quarter_end(year, quarter), iso_date(report_date)
                for metric, value, original in _statement_metrics(row, statement):
                    values.append((code, period, metric, value, original, published, observed,
                                   f"{market.upper()}/OpenAPI/financials/{statement}_{kind}"))
            total += store.upsert("quarterly_financials",
                                  ("code", "period_end", "metric", "value", "original_name",
                                   "published_at", "observed_at", "source"), values)
    total += _derive_official_financial_metrics(store, observed)
    return total


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
