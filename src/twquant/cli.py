"""Windows-friendly commands: python -m twquant.cli --help."""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from .ingest import (DATASETS, OFFICIAL_FEEDS, archive_official_reports,
                     ingest_delistings, ingest_official, ingest_stock_info, now, sync)
from .metrics import financial_metrics
from .sources import FinMind, HttpClient, SourceError, iso_date
from .store import Store
from .yahoo_personal import fetch_personal_backup


def _defaults() -> str:
    return os.environ.get("TWQUANT_DB", "data/twquant.sqlite3")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="TW quant data layer, stage 1")
    p.add_argument("--db", default=_defaults(), help="SQLite path")
    p.add_argument("--token", default=os.environ.get("FINMIND_TOKEN", ""), help="FinMind free token")
    p.add_argument("--interval", type=float, default=12.5, help="seconds between HTTP requests (>=1)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create database")
    info = sub.add_parser("universe", help="refresh current security list and delisting records")
    info.add_argument("--include-delisted", action="store_true")
    prices = sub.add_parser("official", help="latest TWSE/TPEx OpenAPI post-close snapshots")
    prices.add_argument("--market", choices=("twse", "tpex", "both"), default="both")
    feeds = sub.add_parser("official-feeds", help="archive other bulk OpenAPI reports; schema 需驗證")
    feeds.add_argument("--feeds", nargs="+", choices=tuple(OFFICIAL_FEEDS),
                       default=list(OFFICIAL_FEEDS))
    run = sub.add_parser("sync", help="checkpointed FinMind per-stock backfill/update")
    run.add_argument("--codes", nargs="*", help="stock codes; omit to use saved universe")
    run.add_argument("--datasets", nargs="+", choices=tuple(DATASETS), default=list(DATASETS))
    run.add_argument("--start", default=(date.today() - timedelta(days=365)).isoformat())
    run.add_argument("--end", default=date.today().isoformat())
    run.add_argument("--max-requests", type=int, default=250)
    quality = sub.add_parser("check", help="show database counts and a selected price")
    quality.add_argument("--code", default="2330")
    quality.add_argument("--date", help="YYYY-MM-DD, omitted: newest available")
    ev = sub.add_parser("import-disclosures", help="import independently verified official publication dates")
    ev.add_argument("csv_file", type=Path)
    yahoo = sub.add_parser("personal-yahoo", help="local-only Yahoo backup; never publish these rows")
    yahoo.add_argument("--code", required=True)
    yahoo.add_argument("--market", required=True, choices=("twse", "tpex"))
    yahoo.add_argument("--start", required=True)
    yahoo.add_argument("--end", required=True)
    return p


def import_disclosures(store: Store, path: Path) -> int:
    """CSV: dataset,code,period_end,published_at,source_url.

    publication timestamps must be independently verified; never infer them.
    """
    mapping = {"revenue": ("monthly_revenue", "period_end"),
               "financials": ("quarterly_financials", "period_end"),
               "balance": ("quarterly_financials", "period_end")}
    parsed = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        expected = {"dataset", "code", "period_end", "published_at", "source_url"}
        if not expected.issubset(reader.fieldnames or []):
            raise ValueError(f"CSV must include {sorted(expected)}")
        for row in reader:
            group = row["dataset"].strip()
            if group not in mapping:
                raise ValueError(f"Unsupported disclosure type: {group}")
            d, pub = iso_date(row["period_end"]), iso_date(row["published_at"])
            if pub < d or pub > date.today().isoformat():
                raise ValueError(f"Invalid publication date: {row}")
            url = row["source_url"].strip()
            if not url.startswith("https://"):
                raise ValueError("Require official HTTPS evidence URL")
            parsed.append((group, row["code"].strip(), d, pub, url))
    count = 0
    with store.db:
        store.db.execute("""CREATE TABLE IF NOT EXISTS disclosure_evidence (
            dataset TEXT NOT NULL, code TEXT NOT NULL, period_end TEXT NOT NULL,
            published_at TEXT NOT NULL, source_url TEXT NOT NULL,
            imported_at TEXT NOT NULL, PRIMARY KEY(dataset,code,period_end))""")
        for group, code, period, pub, url in parsed:
            table, period_column = mapping[group]
            suffix = " AND metric LIKE 'bs:%'" if group == "balance" else (
                     " AND metric LIKE 'is:%'" if group == "financials" else "")
            stmt = f"UPDATE {table} SET published_at=? WHERE code=? AND {period_column}=?{suffix}"
            updated = store.db.execute(stmt, (pub, code, period)).rowcount
            if not updated:
                raise ValueError(f"No matching stored data: {group} {code} {period}")
            store.db.execute("INSERT OR REPLACE INTO disclosure_evidence VALUES (?,?,?,?,?,?)",
                             (group, code, period, pub, url, now()))
            count += updated
    return count


def check(store: Store, code: str, day: str | None) -> None:
    for table in ("securities", "delistings", "prices", "price_adjusted", "monthly_revenue",
                  "quarterly_financials", "institutional_flows", "margin_balances",
                  "corporate_actions", "official_reports", "sync_errors"):
        n = store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"{table:24} {n:8}")
    sql = ("SELECT code,trade_date,open,high,low,close,volume_shares,source,published_at,"
           "observed_at FROM prices WHERE code=?")
    args = [code]
    if day:
        sql += " AND trade_date=?"
        args.append(iso_date(day))
    sql += " ORDER BY trade_date DESC LIMIT 1"
    row = store.db.execute(sql, args).fetchone()
    print("PRICE:", dict(row) if row else "no matching stored price")
    metrics = financial_metrics(store, code)
    if metrics:
        print("LATEST_FINANCIALS:", metrics[-1])
        if not metrics[-1]["strict_pit_eligible"]:
            print("WARNING: no verified disclosure date; exclude financials from strict PIT backtests")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        with Store(args.db) as store:
            if args.command == "init":
                print("Created", store.path)
            elif args.command == "check":
                check(store, args.code, args.date)
            elif args.command == "import-disclosures":
                print("updated financial/revenue rows:", import_disclosures(store, args.csv_file))
            elif args.command == "personal-yahoo":
                print("personal-only fallback rows:", fetch_personal_backup(
                    store, args.code, args.market, date.fromisoformat(args.start),
                    date.fromisoformat(args.end)))
            else:
                api = FinMind(HttpClient(args.interval), args.token)
                if args.command == "universe":
                    print("tracked listed/OTC securities:", ingest_stock_info(
                        store, api.fetch("TaiwanStockInfo")))
                    if args.include_delisted:
                        print("delisting records:", ingest_delistings(
                            store, api.fetch("TaiwanStockDelisting")))
                elif args.command == "official":
                    for market in (("twse", "tpex") if args.market == "both" else (args.market,)):
                        print(market, "records:", ingest_official(store, api.http, market))
                elif args.command == "official-feeds":
                    print("archived bulk reports:", archive_official_reports(store, api.http, args.feeds))
                elif args.command == "sync":
                    codes = args.codes if args.codes else store.current_codes()
                    if not codes:
                        raise ValueError("No stock codes. Run 'universe' first or pass --codes 2330")
                    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
                    if start > end or end > date.today():
                        raise ValueError("Invalid start/end date")
                    print("inserted/updated rows:", sync(store, api, codes, args.datasets,
                        start, end, max_requests=args.max_requests))
                    print("Completed requests may be fewer than total; rerun to resume")
    except (SourceError, ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
