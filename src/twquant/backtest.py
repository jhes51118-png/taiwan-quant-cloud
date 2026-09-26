"""Event-driven daily portfolio simulation over raw prices, with explicit guards.

Signals are observed after session close. Orders fill no earlier than the next
session. Missing or blocked fills remain unfilled. This module never fabricates
cash dividends or price-limit status from adjusted prices.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

import numpy as np
import pandas as pd


@dataclass
class Result:
    equity: pd.Series
    orders: pd.DataFrame
    metrics: dict
    yearly: pd.Series
    warnings: list[str]
    benchmark: pd.Series | None = None


def _annual_metrics(equity: pd.Series, orders: pd.DataFrame) -> tuple[dict, pd.Series]:
    returns = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
    drawdown = equity / equity.cummax() - 1
    excess = returns.mean()
    std = returns.std(ddof=1)
    downside = np.sqrt(np.mean(np.minimum(returns.to_numpy(), 0) ** 2)) if len(returns) else 0
    yearly = equity.resample("YE").last().pct_change()
    if len(yearly):
        yearly.iloc[0] = equity.loc[:yearly.index[0]].iloc[-1] / equity.iloc[0] - 1
    sells = orders[orders.side == "sell"] if len(orders) else pd.DataFrame()
    metrics = {
        "年化報酬": float(cagr), "最大回撤": float(drawdown.min()),
        "夏普": float(excess / std * sqrt(252)) if std and np.isfinite(std) else np.nan,
        "索提諾": float(excess / downside * sqrt(252)) if downside else np.nan,
        "勝率": float((sells.pnl > 0).mean()) if len(sells) else np.nan,
        "年均換手率": float(orders.notional.sum() / equity.mean() / years) if len(orders) else 0.0,
        "交易筆數": int(len(orders)),
    }
    return metrics, yearly


def _resample_signals(position: pd.DataFrame, freq: str) -> pd.DataFrame:
    if freq not in {"D", "W", "W-FRI", "M", "Q"}:
        raise ValueError("resample 僅支援 D / W / W-FRI / M / Q")
    if freq == "D":
        return position.copy()
    alias = {"W": "W-FRI", "W-FRI": "W-FRI", "M": "M", "Q": "Q"}[freq]
    # A Friday/quarter-end signal is not moved backward if that day was closed.
    # Use the last real signal on or before the calendar period end.
    return position.groupby(position.index.to_period(alias)).tail(1)


def sim(position: pd.DataFrame, prices: pd.DataFrame, *, resample: str = "W",
        execution: str = "open", initial_cash: float = 1_000_000,
        fee_discount: float = 1.0, sell_tax: float = 0.003,
        slippage: float = 0.001, lot_size: int = 1,
        eligibility: pd.DataFrame | None = None,
        cash_dividends: pd.DataFrame | None = None,
        benchmark: pd.Series | None = None) -> Result:
    """Simulate equal-weight target holdings with cash and integer shares.

    prices: MultiIndex (date, code), columns open/high/low/close/volume_shares.
    eligibility: optional same MultiIndex, columns can_buy/can_sell; provide
    validated historical price-limit flags here. Missing status blocks trading.
    cash_dividends: optional same MultiIndex, column cash_per_share and
    published_at, where entitlement is the provided ex-date row. Caller must
    validate event completeness; otherwise pass None and equity is price-only.
    """
    if not isinstance(position, pd.DataFrame) or position.empty:
        raise ValueError("position 需為非空 DataFrame")
    if execution not in {"open", "close"} or not 0 < initial_cash or not 0 <= fee_discount <= 1 or not 0 <= slippage < 1:
        raise ValueError("成交方式、資金、折扣或滑價無效")
    if not 0 <= sell_tax <= .1 or lot_size < 1:
        raise ValueError("稅率或每單位股數無效")
    if not isinstance(prices.index, pd.MultiIndex) or prices.index.nlevels != 2:
        raise ValueError("prices index 必須為 (日期, 股票代號) MultiIndex")
    required = {"open", "high", "low", "close", "volume_shares"}
    if not required <= set(prices.columns):
        raise ValueError(f"prices 缺少欄位：{required - set(prices.columns)}")
    if position.isna().any().any() or not all(pd.api.types.is_bool_dtype(t) for t in position.dtypes):
        raise ValueError("訊號須為明確布林值；不可填補未知歷史成 False")
    position = position.copy()
    position.index = pd.to_datetime(position.index, errors="raise").normalize()
    if not position.index.is_unique or not position.index.is_monotonic_increasing or position.columns.duplicated().any():
        raise ValueError("訊號日期須唯一遞增，代號須唯一")
    px = prices.copy().sort_index()
    px.index = pd.MultiIndex.from_arrays(
        [pd.to_datetime(px.index.get_level_values(0)).normalize(), px.index.get_level_values(1).astype(str)],
        names=["date", "code"])
    if px.index.has_duplicates:
        raise ValueError("重複行情列")
    days = px.index.get_level_values(0).unique().sort_values()
    if len(days) < 2:
        raise ValueError("至少需要兩個交易日行情")
    if not position.index.isin(days).all():
        raise ValueError("訊號日期必須是已知交易日，不能事後回填假日訊號")
    if not position.columns.isin(px.index.get_level_values(1)).all():
        raise ValueError("持股代號缺少行情；不能默默忽略或只回測存活股票")
    if eligibility is None:
        raise ValueError("必須提供經驗證的歷史買賣可成交旗標；不能推測漲跌停")
    if not {"can_buy", "can_sell"} <= set(eligibility.columns):
        raise ValueError("eligibility 須有 can_buy / can_sell")
    if not eligibility.index.equals(prices.index):
        raise ValueError("eligibility 與 prices index 必須完全一致")
    flags = eligibility.copy()
    flags.index = px.index
    if flags[["can_buy", "can_sell"]].isna().any().any():
        raise ValueError("未知的可成交狀態不得視為可成交")
    if cash_dividends is not None and not {"cash_per_share", "published_at"} <= set(cash_dividends.columns):
        raise ValueError("股利須附現金股利與公告日期")
    selected = _resample_signals(position, resample)
    instructions: dict[pd.Timestamp, set[str]] = {}
    for signal_day, holdings in selected.iterrows():
        next_day = days[days > signal_day]
        if len(next_day):
            instructions[next_day[0]] = set(map(str, holdings.index[holdings]))
    cash = float(initial_cash)
    shares: dict[str, int] = {}
    avg_cost: dict[str, float] = {}
    previous_close: dict[str, float] = {}
    orders: list[dict] = []
    values: list[float] = []
    warnings: list[str] = []
    target: set[str] = set()
    fee_rate = 0.001425 * fee_discount
    for day in days:
        day_px = px.loc[day]
        day_flags = flags.loc[day]
        if cash_dividends is not None and day in cash_dividends.index.get_level_values(0):
            for code, event in cash_dividends.loc[day].iterrows():
                if code in shares and pd.notna(event.cash_per_share):
                    cash += shares[code] * float(event.cash_per_share)
        if day in instructions:
            target = instructions[day]
        if target or shares:
            for code in sorted(set(shares) - target):
                if code not in day_px.index:
                    warnings.append(f"{day.date()} {code}: 無報價；保留最後市值直到事件資料補齊")
                    continue
                row = day_px.loc[code]
                f = day_flags.loc[code]
                price = row[execution]
                if not bool(f.can_sell) or pd.isna(price) or price <= 0 or row.volume_shares <= 0:
                    continue
                fill = float(price) * (1 - slippage)
                qty = shares.pop(code)
                proceeds = qty * fill * (1 - fee_rate - sell_tax)
                cash += proceeds
                orders.append({"date": day, "code": code, "side": "sell", "shares": qty,
                               "fill": fill, "notional": qty * fill, "pnl": proceeds - avg_cost.pop(code)})
            pool = sorted(target - set(shares))
            current_value = cash + sum(q * previous_close.get(c, 0) for c, q in shares.items())
            budget = current_value / max(len(target), 1)
            for code in pool:
                if code not in day_px.index:
                    continue
                row = day_px.loc[code]
                f = day_flags.loc[code]
                price = row[execution]
                if not bool(f.can_buy) or pd.isna(price) or price <= 0 or row.volume_shares <= 0:
                    continue
                fill = float(price) * (1 + slippage)
                unit_cost = fill * (1 + fee_rate)
                qty = int(min(cash, budget) // (unit_cost * lot_size)) * lot_size
                if qty <= 0:
                    continue
                cost = qty * unit_cost
                cash -= cost
                shares[code] = qty
                avg_cost[code] = cost
                orders.append({"date": day, "code": code, "side": "buy", "shares": qty,
                               "fill": fill, "notional": qty * fill, "pnl": np.nan})
        value = cash
        for code, qty in shares.items():
            if code in day_px.index and pd.notna(day_px.loc[code, "close"]):
                previous_close[code] = float(day_px.loc[code, "close"])
            if code in previous_close:
                value += qty * previous_close[code]
            else:
                warnings.append(f"{day.date()} {code}: 市值無法計算")
        values.append(value)
    equity = pd.Series(values, index=days, name="equity")
    order_df = pd.DataFrame(orders, columns=["date", "code", "side", "shares", "fill", "notional", "pnl"])
    metrics, yearly = _annual_metrics(equity, order_df)
    if cash_dividends is None:
        warnings.append("未提供經核實的完整現金股利事件，績效為價格加現金部位，不代表含息報酬")
    if benchmark is not None:
        benchmark = benchmark.reindex(days).ffill()
        if benchmark.isna().any():
            warnings.append("0050 基準缺少起始行情；不顯示不完整比較")
            benchmark = None
        else:
            benchmark = benchmark / benchmark.iloc[0] * initial_cash
    return Result(equity, order_df, metrics, yearly, list(dict.fromkeys(warnings)), benchmark)


def walk_forward(position: pd.DataFrame, split: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Separate train and test signals; do not tune parameters on the test period."""
    boundary = pd.Timestamp(split)
    train, test = position.loc[position.index < boundary], position.loc[position.index >= boundary]
    if train.empty or test.empty:
        raise ValueError("訓練與測試期間皆須包含至少一個訊號日")
    return train, test
