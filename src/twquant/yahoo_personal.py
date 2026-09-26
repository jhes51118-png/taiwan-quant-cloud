"""Optional local personal-use backup. NEVER publish Yahoo-sourced rows."""
from __future__ import annotations

from datetime import date, timedelta

from .ingest import now
from .sources import SourceError, number
from .store import Store


def fetch_personal_backup(store: Store, code: str, market: str,
                          start: date, end: date) -> int:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise SourceError("Install optional dependency: pip install -e .[yahoo]") from exc
    if market not in ("twse", "tpex"):
        raise ValueError("market must be twse or tpex")
    suffix = ".TW" if market == "twse" else ".TWO"
    # The yfinance 'end' parameter is exclusive. auto_adjust=False retains raw OHLC.
    frame = yf.Ticker(code + suffix).history(
        start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False, actions=True, interval="1d", raise_errors=True)
    if frame.empty:
        raise SourceError(f"No yfinance rows: {code}{suffix}")
    inserted = 0
    t = now()
    with store.db:
        for idx, row in frame.iterrows():
            d = idx.date().isoformat()
            o, h, l, c = (number(row[k]) for k in ("Open", "High", "Low", "Close"))
            volume = number(row["Volume"], integer=True)
            if None in (o, h, l, c, volume) or min(o, h, l, c) <= 0:
                continue
            if not l <= min(o, c) <= max(o, c) <= h:
                raise SourceError(f"Yahoo invalid OHLC {code} {d}")
            cur = store.db.execute("""INSERT OR IGNORE INTO prices
                (code,trade_date,open,high,low,close,volume_shares,turnover_twd,
                 source,published_at,observed_at,availability)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, d, o, h, l, c, volume, None,
                 "yfinance/personal_only", None, t, "historical_date_unverified"))
            inserted += cur.rowcount
    return inserted
