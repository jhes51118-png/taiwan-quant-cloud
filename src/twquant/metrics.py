"""Derived financial indicators. Values are not PIT-safe without disclosures."""
from __future__ import annotations

from .store import Store


def financial_metrics(store: Store, code: str) -> list[dict]:
    records = store.db.execute(
        "SELECT period_end,metric,value,published_at,original_name "
        "FROM quarterly_financials WHERE code=? ORDER BY period_end", (code,)
    )
    by_period: dict[str, dict] = {}
    for row in records:
        period, metric, value, published, label = row
        group = by_period.setdefault(period, {"period_end": period,
                                              "published_at": published})
        group[metric] = value
        if group["published_at"] is None or published is None:
            group["published_at"] = None
        else:
            group["published_at"] = max(group["published_at"], published)
        # Do not confuse similarly named comprehensive income and balance sheet equity.
        if metric == "bs:EquityAttributableToOwnersOfParent":
            group["_equity_label"] = label or ""
    values = list(by_period.values())
    for i, row in enumerate(values):
        rev, gross = row.get("is:Revenue"), row.get("is:GrossProfit")
        row["eps"] = row.get("is:EPS")
        row["gross_margin"] = gross / rev if rev and gross is not None else None
        row["roe_ttm_estimate"] = None
        if i >= 4:
            net = [x.get("is:IncomeAfterTaxes") for x in values[i - 3:i + 1]]
            current = row.get("bs:EquityAttributableToOwnersOfParent")
            previous = values[i - 4].get("bs:EquityAttributableToOwnersOfParent")
            # Label check guards against accidentally interpreting comprehensive income as equity.
            label = row.get("_equity_label", "")
            if (all(x is not None for x in net) and current is not None and
                previous is not None and current + previous > 0 and
                "權益" in label and "綜合損益" not in label):
                row["roe_ttm_estimate"] = sum(net) / ((current + previous) / 2)
        row["strict_pit_eligible"] = row["published_at"] is not None
        row.pop("_equity_label", None)
        for key in list(row):
            if key.startswith(("is:", "bs:")):
                row.pop(key)
    return values
