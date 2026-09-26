"""Strict as-of signals. Unknown publication dates never become eligible."""
from __future__ import annotations

import pandas as pd

from .store import Store


def price_frame(store: Store, *, public: bool = True) -> pd.DataFrame:
    clause = " AND source IN ('TWSE/OpenAPI','TPEX/OpenAPI')" if public else ""
    rows = store.db.execute("SELECT trade_date AS date,code,open,high,low,close,volume_shares "
                            "FROM prices WHERE close>0" + clause + " ORDER BY trade_date,code").fetchall()
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume_shares"],
                            index=pd.MultiIndex.from_arrays([[], []], names=["date", "code"]))
    df = pd.DataFrame([dict(r) for r in rows])
    df["date"] = pd.to_datetime(df.date)
    return df.set_index(["date", "code"]).sort_index()


def _latest_known(rows: pd.DataFrame, dates: pd.DatetimeIndex, codes: list[str], value: str) -> pd.DataFrame:
    out = pd.DataFrame(index=dates, columns=codes, dtype=float)
    if rows.empty:
        return out
    rows = rows.dropna(subset=["published_at", value]).copy()
    if rows.empty:
        return out
    rows["published_at"] = pd.to_datetime(rows.published_at)
    # Announcement dated D is only tradeable from the next session.
    for code, group in rows.groupby("code"):
        if code not in codes:
            continue
        group = group.sort_values(["published_at", "period_end"])
        series = group.drop_duplicates("published_at", keep="last").set_index("published_at")[value]
        out[code] = series.reindex(dates, method="ffill").shift(1)
    return out


def signals(store: Store, strategy: str, *, ma_days: int = 20, top_n: int = 5,
            public: bool = True) -> tuple[pd.DataFrame, str]:
    px = price_frame(store, public=public)
    if px.empty:
        raise ValueError("目前沒有可公開展示的歷史行情；請先完成雲端排程抓取")
    close = px.close.unstack("code").sort_index()
    dates, codes = close.index, list(close.columns)
    if len(dates) < ma_days + 2:
        raise ValueError(f"至少需要 {ma_days+2} 個交易日，目前僅有 {len(dates)} 日")
    ma = close.rolling(ma_days, min_periods=ma_days).mean()
    if strategy == "均線趨勢":
        rank = (close / ma - 1).where(close > ma)
        explanation = f"收盤價高於 {ma_days} 日均線；依乖離程度排序"
    else:
        public_filter = " WHERE source NOT LIKE 'FinMind/%'" if public else ""
        revenue_rows = pd.read_sql_query("SELECT code,period_end,published_at,revenue_twd FROM monthly_revenue" + public_filter, store.db)
        fin_rows = pd.read_sql_query("SELECT code,period_end,published_at,metric,value FROM quarterly_financials" + public_filter, store.db)
        revenue = pd.DataFrame(columns=["code", "period_end", "published_at", "momentum"])
        if not revenue_rows.empty:
            revenue_rows = revenue_rows.sort_values(["code", "period_end"])
            revenue_rows["momentum"] = revenue_rows.groupby("code").revenue_twd.pct_change(12, fill_method=None)
            revenue = revenue_rows
        momentum = _latest_known(revenue, dates, codes, "momentum")
        eps = fin_rows[fin_rows.metric == "is:EPS"].copy()
        if not eps.empty:
            eps = eps.sort_values(["code", "period_end"])
            eps["ttm"] = eps.groupby("code").value.transform(lambda s: s.rolling(4, min_periods=4).sum())
        ttm = _latest_known(eps, dates, codes, "ttm")
        # This is a conservative proxy, not audited TTM ROE.
        roe = fin_rows[fin_rows.metric == "is:ROE"].rename(columns={"value": "roe"})
        known_roe = _latest_known(roe, dates, codes, "roe")
        flows = pd.read_sql_query("SELECT code,trade_date AS period_end,published_at, "
                                  "SUM(buy_shares-sell_shares) AS net FROM institutional_flows "
                                  + ("WHERE source NOT LIKE 'FinMind/%' " if public else "") +
                                  "GROUP BY code,trade_date,published_at", store.db)
        if not flows.empty:
            flows = flows.sort_values(["code", "period_end"])
            flows["streak"] = flows.groupby("code").net.transform(
                lambda s: (s > 0).astype(int).rolling(3, min_periods=3).sum())
        streak = _latest_known(flows, dates, codes, "streak")
        if strategy == "營收動能":
            rank = momentum.where(momentum > 0)
            explanation = "僅使用公告日期已核實的月營收；比較去年同月成長率"
        elif strategy == "低本益比＋高 ROE":
            pe = close / ttm.where(ttm > 0)
            rank = (1 / pe).where((known_roe > 0) & (pe > 0) & (pe < 30))
            explanation = "需已公布四季 EPS 和可核實的 ROE；缺資料的股票不會入選"
        elif strategy == "法人連買":
            rank = streak.where(streak == 3) * (close / ma)
            explanation = "近三個有資料日法人淨買超，且各日公布日期已核實"
        elif strategy == "多因子排名":
            a = momentum.rank(axis=1, pct=True)
            b = (close / ma - 1).rank(axis=1, pct=True)
            c = (ttm / close).rank(axis=1, pct=True)
            rank = ((a + b + c) / 3).where(a.notna() & b.notna() & c.notna())
            explanation = "已核實公布日之營收動能、四季 EPS 殖利近似值、均線趨勢等權"
        else:
            raise ValueError("未知策略")
    selected = rank.rank(axis=1, ascending=False, method="first").le(top_n) & rank.notna()
    return selected.fillna(False).astype(bool), explanation


def weekly_recommendations(store: Store, strategy: str, *, top_n: int = 5,
                           ma_days: int = 20, public: bool = True) -> pd.DataFrame:
    position, reason = signals(store, strategy, top_n=top_n, ma_days=ma_days, public=public)
    # Friday after close: this latest available EOD signal is for the next session.
    selected = list(position.columns[position.iloc[-1]])
    if not selected:
        return pd.DataFrame(columns=["代號", "名稱", "入選理由", "建議權重"])
    marks = ",".join("?" for _ in selected)
    allowed = " AND source IN ('TWSE/OpenAPI','TPEX/OpenAPI')" if public else ""
    names = {r[0]: r[1] for r in store.db.execute(
        f"SELECT code,name FROM securities WHERE code IN ({marks})" + allowed, selected)}
    return pd.DataFrame([{"代號": c, "名稱": names.get(c, "名稱待補"),
                          "入選理由": reason, "建議權重": round(1 / len(selected), 4)} for c in selected])
