"""GitHub Actions entry point. No secrets are printed or persisted."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from twquant.ingest import (ingest_official, ingest_official_financials,
                            ingest_official_margin, ingest_official_revenue,
                            ingest_official_tpex_institutions)
from twquant.sources import HttpClient, SourceError
from twquant.store import Store
from twquant.strategies import weekly_recommendations


def telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("Telegram secrets unavailable; skip notification")
        return
    req = Request(f"https://api.telegram.org/bot{token}/sendMessage",
                  data=urlencode({"chat_id": chat, "text": message[:3800]}).encode(), method="POST")
    try:
        with urlopen(req, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError("Telegram HTTP error")
    except Exception as exc:
        raise RuntimeError("Telegram notification failed; check bot and chat ID") from exc


def main() -> None:
    dbpath = Path(os.environ.get("TWQUANT_DB", "data/twquant.sqlite3"))
    http = HttpClient(min_interval=2.0, retries=2)
    taipei = datetime.now(ZoneInfo("Asia/Taipei"))
    successes = 0
    with Store(dbpath) as store:
        latest_by_market: dict[str, str] = {}
        for market in ("twse", "tpex"):
            try:
                n = ingest_official(store, http, market)
                print(f"{market}: {n} official daily rows")
                successes += n
                latest_by_market[market] = store.db.execute(
                    "SELECT MAX(trade_date) FROM prices WHERE source=?",
                    (f"{market.upper()}/OpenAPI",),
                ).fetchone()[0]
            except SourceError as exc:
                print(f"{market}: fetch/schema failed: {exc}")
        if successes == 0:
            raise RuntimeError("No official market snapshot saved; retry scheduled workflow")

        # Supplementary feeds are independent: one schema/network failure must
        # not discard the verified daily quote snapshot or block other feeds.
        for market in ("twse", "tpex"):
            jobs = (
                ("monthly revenue", lambda m=market: ingest_official_revenue(store, http, m)),
                ("margin balances", lambda m=market: ingest_official_margin(
                    store, http, m, latest_quote_date=latest_by_market.get(m))),
                ("financial metrics", lambda m=market: ingest_official_financials(store, http, m)),
            )
            for label, job in jobs:
                try:
                    print(f"{market} {label}: {job()} rows")
                except (SourceError, ValueError) as exc:
                    print(f"{market} {label}: fetch/schema failed: {exc}")
        try:
            print(f"tpex institutions: {ingest_official_tpex_institutions(store, http)} rows")
        except (SourceError, ValueError) as exc:
            print(f"tpex institutions: fetch/schema failed: {exc}")
        print("twse institutions: 需驗證 — no per-stock feed in the current TWSE OpenAPI Swagger")

        latest = store.db.execute("SELECT MAX(trade_date) FROM prices WHERE source IN ('TWSE/OpenAPI','TPEX/OpenAPI')").fetchone()[0]
        if taipei.weekday() == 4 and taipei.hour >= 18 and latest == taipei.date().isoformat():
            try:
                rec = weekly_recommendations(store, "均線趨勢")
                if len(rec):
                    day = store.db.execute("SELECT MAX(trade_date) FROM prices WHERE source='TWSE/OpenAPI'").fetchone()[0]
                    lines = [f"台股每週研究清單（{day} 收盤）"]
                    lines.extend(f"{r['代號']} {r['名稱']}：{r['建議權重']:.0%}｜{r['入選理由']}"
                                 for _, r in rec.iterrows())
                    telegram("\n".join(lines) + "\n回測結果不保證未來獲利。")
                else:
                    print("No eligible weekly holdings; no notification")
            except ValueError as exc:
                print(f"Weekly recommendation unavailable: {exc}")
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(f"Update complete: {successes} rows")


if __name__ == "__main__":
    main()
