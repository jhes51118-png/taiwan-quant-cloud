"""Public Streamlit website. The server never exposes personal-only data."""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from twquant.backtest import sim, walk_forward
from twquant.store import Store
from twquant.strategies import price_frame, signals, weekly_recommendations


st.set_page_config(page_title="台股量化實驗室", page_icon="📈", layout="wide")
st.title("台股量化實驗室")
st.caption("免費公開研究工具 · 台灣證券交易所／櫃買中心盤後資料 · 最後觀測時間依畫面標示")

DB = Path(os.environ.get("TWQUANT_DB", "data/twquant.sqlite3"))


@st.cache_resource
def connect(path: str) -> Store:
    return Store(path)


store = connect(str(DB))
page = st.sidebar.radio("功能", ["資料總覽", "個股查詢", "策略回測", "每週推薦", "持股追蹤", "風險工具", "盤中報價"])
st.sidebar.caption("網站公開顯示僅使用官方 OpenAPI 來源。數據缺漏或來源未授權的功能會標示原因。")


def prices() -> pd.DataFrame:
    return price_frame(store, public=True)


def empty_note() -> None:
    st.info("官方盤後資料尚未匯入。雲端排程首次成功執行後會顯示；歷史回測須有足夠交易日。")


if page == "資料總覽":
    latest = store.db.execute("SELECT MAX(trade_date), COUNT(DISTINCT trade_date),COUNT(DISTINCT code) "
                              "FROM prices WHERE source IN ('TWSE/OpenAPI','TPEX/OpenAPI')").fetchone()
    a, b, c = st.columns(3)
    a.metric("最近盤後交易日", latest[0] or "待更新")
    b.metric("累積交易日", latest[1])
    c.metric("股票代號", latest[2])
    st.write("資料庫每晚由免費 GitHub Actions 更新；排程可能延遲，以上日期是實際保存的交易日。")
    st.warning("歷史除權息、下市股、漲跌停旗標與公告日期的完整性尚未建立；沒有這些資料時不會宣稱嚴格含息或無偏差回測。")
    st.markdown("資料來源：[TWSE OpenAPI](https://openapi.twse.com.tw/) · [TPEx OpenAPI](https://www.tpex.org.tw/openapi/)。")

elif page == "個股查詢":
    px = prices()
    if px.empty:
        empty_note()
    else:
        names = {r[0]: r[1] for r in store.db.execute(
            "SELECT code,name FROM securities WHERE source IN ('TWSE/OpenAPI','TPEX/OpenAPI')")}
        code = st.selectbox("代號", list(px.index.get_level_values("code").unique()),
                            format_func=lambda c: f"{c} {names.get(c, '')}")
        frame = px.xs(code, level="code")
        graph = go.Figure(go.Candlestick(x=frame.index, open=frame.open, high=frame.high,
                                          low=frame.low, close=frame.close))
        graph.add_trace(go.Scatter(x=frame.index, y=frame.close.rolling(20).mean(), name="20 日均線"))
        graph.update_layout(title=f"{code} 原始價 K 線", xaxis_rangeslider_visible=False)
        st.plotly_chart(graph, width="stretch")
        st.caption("股價為未還原價；量為股，非張。歷史日期僅涵蓋雲端排程已保存的日期。")
        st.dataframe(frame.tail(30), width="stretch")

elif page in {"策略回測", "每週推薦"}:
    strategies = ["均線趨勢", "營收動能", "低本益比＋高 ROE", "法人連買", "多因子排名"]
    strategy = st.selectbox("策略", strategies)
    ma_days = st.number_input("均線日數", 5, 250, 20)
    top_n = st.number_input("最多持股檔數", 1, 30, 5)
    if page == "每週推薦":
        try:
            rec = weekly_recommendations(store, strategy, top_n=int(top_n), ma_days=int(ma_days))
            last = store.db.execute("SELECT MAX(trade_date) FROM prices WHERE source IN ('TWSE/OpenAPI','TPEX/OpenAPI')").fetchone()[0]
            st.caption(f"訊號基準：{last or '無'} 收盤。僅供下個交易日研究；週五 22:00 台灣時間的排程產生每週清單。")
            st.dataframe(rec, width="stretch", hide_index=True)
            if rec.empty:
                st.info("本策略沒有合格股票；基本面及法人資料若缺核實的公布日，會被嚴格排除。")
        except ValueError as exc:
            st.info(str(exc))
    else:
        execution = st.selectbox("T+1 成交價", ["open", "close"], format_func={"open":"開盤", "close":"收盤"}.get)
        freq = st.selectbox("再平衡", ["D", "W", "W-FRI", "M", "Q"], index=1)
        fee_discount = st.slider("手續費折扣係數", 0.0, 1.0, 1.0, .05)
        slippage = st.slider("每次成交滑價", 0.0, .02, .001, .0005)
        study_mode = st.checkbox("研究模式：未知漲跌停狀態以可成交估算（結果會高估可執行性）")
        flag_file = st.file_uploader("或上傳核實的每日成交旗標 CSV：date,code,can_buy,can_sell", type="csv")
        benchmark_file = st.file_uploader("可選：已核實的 0050 含息指數 CSV：date,total_return_index", type="csv", key="benchmark")
        split_enabled = st.checkbox("另顯示樣本內外切分")
        split_date = st.date_input("樣本外起始日", value=pd.Timestamp.today().date(), disabled=not split_enabled)
        if st.button("執行回測", type="primary"):
            try:
                positions, reason = signals(store, strategy, top_n=int(top_n), ma_days=int(ma_days))
                px = prices()
                if flag_file is not None:
                    raw = pd.read_csv(flag_file, dtype={"code": str})
                    if set(raw.columns) != {"date", "code", "can_buy", "can_sell"} or not raw[["can_buy", "can_sell"]].isin([0, 1, True, False]).all().all():
                        raise ValueError("CSV 欄位／旗標需完整且為 0 或 1")
                    raw["date"] = pd.to_datetime(raw.date)
                    flags = raw.set_index(["date", "code"])[["can_buy", "can_sell"]].reindex(px.index)
                    if flags.isna().any().any():
                        raise ValueError("旗標須覆蓋所有行情列")
                elif study_mode:
                    tradable = px.volume_shares.gt(0) & px.open.notna() & px.close.notna()
                    flags = pd.DataFrame({"can_buy": tradable, "can_sell": tradable}, index=px.index)
                else:
                    raise ValueError("嚴格回測須上傳歷史漲跌停與停牌可成交旗標；或明確啟用研究模式")
                reference = None
                if benchmark_file is not None:
                    bench = pd.read_csv(benchmark_file)
                    if set(bench.columns) != {"date", "total_return_index"}:
                        raise ValueError("0050 含息 CSV 欄位需為 date,total_return_index")
                    bench["date"] = pd.to_datetime(bench.date)
                    reference = bench.set_index("date").total_return_index.astype(float).sort_index()
                    if (reference <= 0).any() or reference.index.has_duplicates:
                        raise ValueError("0050 指數須正值且日期唯一")
                if split_enabled:
                    walk_forward(positions, str(split_date))
                result = sim(positions, px, resample=freq, execution=execution, fee_discount=fee_discount,
                             slippage=slippage, eligibility=flags, benchmark=reference)
                st.write(reason)
                curve = result.equity.to_frame("策略權益")
                if result.benchmark is not None:
                    curve["0050 含息指數（上傳）"] = result.benchmark
                st.line_chart(curve)
                st.dataframe(pd.DataFrame([result.metrics]).T, width="stretch")
                st.write("各年績效")
                st.dataframe(result.yearly.rename("報酬率"))
                st.write("成交紀錄")
                st.dataframe(result.orders, width="stretch")
                if split_enabled:
                    for label, segment in (("樣本內", result.equity.loc[result.equity.index < str(split_date)]),
                                           ("樣本外", result.equity.loc[result.equity.index >= str(split_date)])):
                        if len(segment) >= 2:
                            st.metric(f"{label}區間報酬（固定參數）", f"{segment.iloc[-1]/segment.iloc[0]-1:.2%}")
                    st.caption("切分用於報告兩期間結果；此範例沒有自動最佳化參數，請勿用樣本外資料反覆調參。")
                for warning in result.warnings:
                    st.warning(warning)
                if study_mode and flag_file is None:
                    st.warning("漲跌停未知；停牌僅依零成交量推測。研究模式不可視為可實際成交的回測。")
                st.warning("尚無系統內建完整 0050 含息、除權息與歷史下市股資料：未上傳核實基準時無法顯示嚴格總報酬比較，也無法證明消除存活者偏差。")
            except (ValueError, KeyError, TypeError) as exc:
                st.error(str(exc))

elif page == "持股追蹤":
    st.write("追蹤你的持股（這個瀏覽器工作階段內，不存入公開資料庫）")
    typed = st.text_input("代號，以逗號分隔", "2330,2317")
    codes = [c.strip() for c in typed.split(",") if c.strip() and len(c.strip()) <= 6][:30]
    px = prices()
    if px.empty:
        empty_note()
    else:
        rows = px.reset_index().sort_values("date").drop_duplicates("code", keep="last")
        st.dataframe(rows[rows.code.isin(codes)][["code", "date", "close", "volume_shares"]],
                     width="stretch", hide_index=True)

elif page == "風險工具":
    st.write("夏普：日平均報酬／日標準差 × √252。索提諾：日平均報酬／下檔偏差 × √252。年化利率基準暫設 0。")
    uploaded = st.file_uploader("上傳每日權益 CSV，欄位 date,equity", type="csv")
    if uploaded:
        try:
            frame = pd.read_csv(uploaded)
            frame["date"] = pd.to_datetime(frame.date)
            s = frame.set_index("date").equity.astype(float).sort_index()
            ret = s.pct_change().dropna()
            if len(ret) < 2 or (s <= 0).any():
                raise ValueError("須有至少 3 天正值權益")
            st.metric("夏普", f"{ret.mean()/ret.std()*252**.5:.2f}")
            st.metric("最大回撤", f"{(s/s.cummax()-1).min():.2%}")
            st.line_chart(s)
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            st.error(str(exc))

else:
    st.info("公開網站不轉播券商 Shioaji 或 TWSE MIS 的盤中報價，因為需要核對對外再散布授權。個人可在券商授權介面查詢；此站顯示最近一筆官方盤後資料。")
    rows = store.db.execute("SELECT p.code,s.name,p.trade_date,p.close FROM prices p "
                            "LEFT JOIN securities s ON s.code=p.code WHERE p.source IN ('TWSE/OpenAPI','TPEX/OpenAPI') "
                            "AND p.trade_date=(SELECT MAX(trade_date) FROM prices) ORDER BY p.code LIMIT 100").fetchall()
    st.dataframe(pd.DataFrame([dict(r) for r in rows]), hide_index=True, width="stretch")

st.divider()
st.caption("來源：臺灣證券交易所、證券櫃檯買賣中心各 OpenAPI。資料於取得日記錄，不保證不間斷。回測結果不保證未來獲利。")
