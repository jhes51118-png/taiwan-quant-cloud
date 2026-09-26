"""SQLite storage. All source values retain provenance and time semantics."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS securities (
    code TEXT PRIMARY KEY, name TEXT NOT NULL, market TEXT NOT NULL,
    info_date TEXT, industry TEXT, last_seen_at TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_history (
    code TEXT NOT NULL, market TEXT NOT NULL, source_date TEXT NOT NULL,
    name TEXT NOT NULL, observed_at TEXT NOT NULL,
    PRIMARY KEY(code, market, source_date)
);
CREATE TABLE IF NOT EXISTS delistings (
    code TEXT NOT NULL, event_date TEXT NOT NULL, payload TEXT NOT NULL,
    observed_at TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY(code, event_date)
);
CREATE TABLE IF NOT EXISTS prices (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume_shares INTEGER NOT NULL, turnover_twd REAL,
    source TEXT NOT NULL, published_at TEXT, observed_at TEXT NOT NULL,
    availability TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY(code, trade_date)
);
CREATE TABLE IF NOT EXISTS monthly_revenue (
    code TEXT NOT NULL, period_end TEXT NOT NULL, revenue_twd REAL NOT NULL,
    published_at TEXT, provider_observed_on TEXT, observed_at TEXT NOT NULL,
    source TEXT NOT NULL, PRIMARY KEY(code, period_end)
);
CREATE TABLE IF NOT EXISTS quarterly_financials (
    code TEXT NOT NULL, period_end TEXT NOT NULL,
    metric TEXT NOT NULL, value REAL NOT NULL, original_name TEXT,
    published_at TEXT, observed_at TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY(code, period_end, metric)
);
CREATE TABLE IF NOT EXISTS institutional_flows (
    code TEXT NOT NULL, trade_date TEXT NOT NULL, institution TEXT NOT NULL,
    buy_shares INTEGER NOT NULL, sell_shares INTEGER NOT NULL,
    published_at TEXT, observed_at TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY(code, trade_date, institution)
);
CREATE TABLE IF NOT EXISTS margin_balances (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    margin_balance_lots INTEGER, short_balance_lots INTEGER,
    payload TEXT NOT NULL, published_at TEXT,
    observed_at TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY(code, trade_date)
);
CREATE TABLE IF NOT EXISTS corporate_actions (
    code TEXT NOT NULL, ex_date TEXT NOT NULL, before_price REAL,
    after_price REAL, action_value REAL, action_kind TEXT,
    published_at TEXT, observed_at TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY(code, ex_date)
);
CREATE TABLE IF NOT EXISTS price_adjusted (
    code TEXT NOT NULL, trade_date TEXT NOT NULL, adjusted_close REAL NOT NULL,
    adjustment_factor REAL NOT NULL, method TEXT NOT NULL,
    calculated_at TEXT NOT NULL, PRIMARY KEY(code, trade_date)
);
CREATE TABLE IF NOT EXISTS sync_state (
    dataset TEXT NOT NULL, code TEXT NOT NULL, last_success_end TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY(dataset, code)
);
CREATE TABLE IF NOT EXISTS sync_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, dataset TEXT NOT NULL,
    code TEXT NOT NULL, occurred_at TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS official_reports (
    feed TEXT NOT NULL, retrieved_on TEXT NOT NULL, payload_blob BLOB NOT NULL,
    observed_at TEXT NOT NULL, published_at TEXT,
    PRIMARY KEY(feed, retrieved_on)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(trade_date);
CREATE INDEX IF NOT EXISTS idx_revenue_period ON monthly_revenue(period_end);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(DDL)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def upsert(self, table: str, columns: tuple[str, ...], rows: Iterable[tuple]) -> int:
        # Table/columns are only ever constants defined in this package.
        if table not in {"securities", "security_history", "delistings", "prices",
                         "monthly_revenue", "quarterly_financials", "institutional_flows",
                         "margin_balances", "corporate_actions", "price_adjusted",
                         "official_reports"}:
            raise ValueError("Unsupported table")
        values = list(rows)
        if not values:
            return 0
        names = ",".join(columns)
        marks = ",".join("?" for _ in columns)
        keys = {
            "securities": ("code",), "security_history": ("code", "market", "source_date"),
            "delistings": ("code", "event_date"), "prices": ("code", "trade_date"),
            "monthly_revenue": ("code", "period_end"),
            "quarterly_financials": ("code", "period_end", "metric"),
            "institutional_flows": ("code", "trade_date", "institution"),
            "margin_balances": ("code", "trade_date"),
            "corporate_actions": ("code", "ex_date"),
            "price_adjusted": ("code", "trade_date"),
            "official_reports": ("feed", "retrieved_on"),
        }[table]
        updates = []
        for col in columns:
            if col in keys:
                continue
            if col == "published_at" and table == "prices":
                expr = f"COALESCE({table}.{col},excluded.{col})"
            elif col == "published_at":
                expr = f"COALESCE(excluded.{col},{table}.{col})"
            elif col == "observed_at" and table != "official_reports":
                expr = f"{table}.{col}"  # first time our system observed the row
            else:
                expr = f"excluded.{col}"
            updates.append(f"{col}={expr}")
        query = (f"INSERT INTO {table} ({names}) VALUES ({marks}) ON CONFLICT({','.join(keys)}) "
                 f"DO UPDATE SET {','.join(updates)}")
        with self.db:
            self.db.executemany(query, values)
        return len(values)

    def checkpoint(self, dataset: str, code: str) -> str | None:
        row = self.db.execute(
            "SELECT last_success_end FROM sync_state WHERE dataset=? AND code=?",
            (dataset, code),
        ).fetchone()
        return row[0] if row else None

    def mark_success(self, dataset: str, code: str, end: str, now: str) -> None:
        with self.db:
            self.db.execute("""INSERT INTO sync_state VALUES (?,?,?,?)
                ON CONFLICT(dataset,code) DO UPDATE SET
                last_success_end=excluded.last_success_end, updated_at=excluded.updated_at""",
                (dataset, code, end, now))

    def mark_error(self, dataset: str, code: str, now: str, error: Exception) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO sync_errors(dataset,code,occurred_at,message) VALUES (?,?,?,?)",
                (dataset, code, now, f"{type(error).__name__}: {error}"[:1000]),
            )

    def current_codes(self) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT code FROM securities WHERE market IN ('twse','tpex') "
            "AND code GLOB '[0-9][0-9][0-9][0-9]' AND code NOT LIKE '0%' ORDER BY code"
        )]
