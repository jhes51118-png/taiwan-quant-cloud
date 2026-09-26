"""Free HTTP sources. Schemas are validated before anything is stored."""
from __future__ import annotations

import json
import random
import time
from datetime import date
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


FINMIND = "https://api.finmindtrade.com/api/v4/data"
TWSE = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"


class SourceError(RuntimeError):
    pass


class RateLimitError(SourceError):
    pass


class HttpClient:
    def __init__(self, min_interval: float = 12.5, retries: int = 3):
        if min_interval < 1:
            raise ValueError("min_interval must be >= 1 second")
        self.min_interval = min_interval
        self.retries = retries
        self.last_request = 0.0

    def get_json(self, url: str, params: dict[str, str] | None = None,
                 headers: dict[str, str] | None = None) -> Any:
        target = f"{url}?{urlencode(params)}" if params else url
        for attempt in range(self.retries + 1):
            delay = self.min_interval - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()
            req = Request(target, headers={"User-Agent": "twquant-personal/0.1",
                                           "Accept": "application/json", **(headers or {})})
            try:
                with urlopen(req, timeout=25) as response:
                    data = json.load(response)
                return data
            except HTTPError as e:
                if e.code in (402, 429):
                    # A hard hourly quota must not be retried in a tight loop.
                    raise RateLimitError(f"HTTP {e.code}: quota reached; resume later") from e
                if e.code not in (500, 502, 503, 504) or attempt == self.retries:
                    raise SourceError(f"HTTP {e.code}: {url}") from e
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                if attempt == self.retries:
                    raise SourceError(f"Network/JSON error at {url}: {e}") from e
            time.sleep(min(30, 2 ** attempt + random.uniform(0, 0.5)))
        raise AssertionError("Unreachable")


class FinMind:
    def __init__(self, http: HttpClient, token: str = ""):
        self.http = http
        self.token = token

    def fetch(self, dataset: str, code: str | None = None,
              start: str | None = None, end: str | None = None) -> list[dict]:
        params = {"dataset": dataset}
        if code:
            params["data_id"] = code
        if start:
            params["start_date"] = start
        if end:
            params["end_date"] = end
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = self.http.get_json(FINMIND, params, headers)
        if isinstance(payload, dict) and payload.get("status") in (402, 429):
            raise RateLimitError(f"FinMind {dataset}: quota reached; resume later")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise SourceError(f"Unexpected FinMind {dataset} response")
        status = payload.get("status")
        if status not in (None, 200):
            raise SourceError(f"FinMind {dataset}: status={status}, msg={payload.get('msg')}")
        if any(not isinstance(row, dict) for row in payload["data"]):
            raise SourceError(f"FinMind {dataset}: non-object row")
        return payload["data"]


def iso_date(value: Any) -> str:
    """Date only; ROC dates accepted for official quote snapshots."""
    s = str(value).strip()
    if "/" in s and len(s.split("/")) == 3:
        y, m, d = (int(x) for x in s.split("/"))
        return date(y + 1911 if y < 1911 else y, m, d).isoformat()
    if s.isdigit() and len(s) in (7, 8):
        if len(s) == 7:
            return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:])).isoformat()
        return date(int(s[:4]), int(s[4:6]), int(s[6:])).isoformat()
    return date.fromisoformat(s[:10]).isoformat()


def number(value: Any, *, integer: bool = False) -> float | int | None:
    s = str(value).replace(",", "").strip() if value is not None else ""
    if s in ("", "--", "-", "N/A", "None", "nan"):
        return None
    n = float(s)
    if not (-1e30 < n < 1e30):
        raise ValueError(f"Nonfinite number: {s}")
    if integer and not n.is_integer():
        raise ValueError(f"Expected integer: {s}")
    return int(n) if integer else n


def official_snapshot(http: HttpClient, market: str) -> list[dict]:
    url = TWSE if market == "twse" else TPEX if market == "tpex" else None
    if not url:
        raise ValueError("unknown market")
    rows = http.get_json(url)
    if not isinstance(rows, list) or not rows:
        raise SourceError(f"{market} OpenAPI schema/response needs verification")
    # Explicit current OpenAPI field mapping; reject changes instead of guessing.
    mapping = ({"code": "Code", "date": "Date", "name": "Name",
                "open": "OpeningPrice", "high": "HighestPrice", "low": "LowestPrice",
                "close": "ClosingPrice", "volume": "TradeVolume", "turnover": "TradeValue"}
               if market == "twse" else
               {"code": "SecuritiesCompanyCode", "date": "Date", "name": "CompanyName",
                "open": "Open", "high": "High", "low": "Low", "close": "Close",
                "volume": "TradingShares", "turnover": "TransactionAmount"})
    needed = set(mapping.values())
    if not needed.issubset(rows[0]):
        raise SourceError(f"{market} OpenAPI columns need verification: {sorted(needed-set(rows[0]))}")
    result = []
    for r in rows:
        code = str(r[mapping["code"]]).strip()
        if len(code) != 4 or not code.isdigit() or code.startswith("0"):
            continue  # ordinary shares only; excludes ETFs, warrants, indices
        trade_day = iso_date(r[mapping["date"]])
        if trade_day > date.today().isoformat():
            raise SourceError("Future trade date from OpenAPI")
        result.append({"code": code, "date": trade_day, "name": r[mapping["name"]],
                       "open": number(r[mapping["open"]]), "high": number(r[mapping["high"]]),
                       "low": number(r[mapping["low"]]), "close": number(r[mapping["close"]]),
                       "volume": number(r[mapping["volume"]], integer=True) or 0,
                       "turnover": number(r[mapping["turnover"]]), "market": market})
    if not result:
        raise SourceError(f"{market}: no four-digit equities; verify response")
    return result
