# 台股量化實驗室（Python 3.11+、免費雲端網站）

使用者只需在瀏覽器開啟網站，不必在自己的電腦安裝 Python。網站目前由 Render 免費方案執行 Streamlit；GitHub Actions 平日台灣時間 22:17 保存 TWSE/TPEx 官方 OpenAPI 最新快照。公開網站只讀取官方來源的資料；FinMind 與 yfinance 個人研究資料不展示於公開網站。

## 目錄

```text
app.py                         Streamlit 網站
src/twquant/store.py            SQLite、資料來源與公布時間
src/twquant/sources.py          免費 API、重試、請求限速
src/twquant/ingest.py           增量抓取、官方快照、FinMind 個人研究
src/twquant/metrics.py          財報衍生指標
src/twquant/strategies.py       五種策略、公告日檢查、每週選股
src/twquant/backtest.py         T+1 回測及交易限制
src/twquant/yahoo_personal.py  僅限個人的 Yahoo 備援
scripts/cloud_update.py         官方每日資料與 Telegram 選配
.github/workflows/update.yml    免費平日雲端排程
tests/                          單元測試
```

## 網站部署（不需在電腦安裝）

1. 將此專案放進自己 GitHub 儲存庫。公開資料庫 `data/twquant.sqlite3` 由排程建立；**不要提交 token、私人持股或個人研究資料**。
2. 在 GitHub Actions 頁面手動執行 `Update official EOD data` 一次，確認 TWSE 與 TPEx 結果、資料庫是否提交。首次只有當日行情，均線策略至少需要 22 個交易日；不會憑空產生歷史行情。
3. 本專案目前部署在 Render：Build Command 為 `pip install -e .`，Start Command 為 `streamlit run app.py --server.address 0.0.0.0 --server.port $PORT`。免費服務休眠後第一次開啟可能延遲 50 秒以上。
4. 可選在儲存庫 Settings → Secrets and variables → Actions 新增 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_CHAT_ID`；沒有這兩個值時，網站照常更新但不推播。不要把機密寫到檔案。

Render 免費容器的本機檔案不是永久資料庫；只有 GitHub Actions **提交回儲存庫**的官方快照能跨重啟保存。GitHub 排程可能延後／停用，網站有最近交易日標示。GitHub 儲存庫及網站公開可見，不適合個人交易紀錄與券商憑證。

## 功能和目前邊界

| 功能 | 行為 |
| --- | --- |
| 個股查詢 | 官方已保存的盤後 K 線、20 日均線，標示原始未還原價格 |
| 策略 | 均線、營收動能、低本益比＋高 ROE、法人連買、多因子排名；基本面及法人公告日未知時排除 |
| 回測 | `twquant.backtest.sim(position, prices, resample='W', eligibility=...)`，支援 D/W/W-FRI/M/Q，T 收盤訊號於 T+1 開盤或收盤執行，分別計算買賣成本、稅和滑價 |
| 交易限制 | 沒報價或零成交量不成交；嚴格模式要求逐日已核實 `can_buy`/`can_sell`，未知漲跌停狀態不得自動通過。研究模式會明確警告。下市股票缺結算事件時維持最後估值並警告 |
| 週報 | 每週五晚有足夠資料時產生持股代號、名稱、理由和等權權重；有 Bot 憑證時才發送 Telegram |
| 0050 含息 | 尚無完整且可公開再利用的股利事件與調整序列，網站明示不可比較，不宣稱已完成 |
| 即時價／模擬下單 | 公開網站不轉播個人 Shioaji 或 MIS 即時價；模擬下單需券商個人環境，這個公開網站不持有任何券商憑證 |

**資料層現況**：雲端排程會保存上市櫃官方盤後價、月營收、六類產業財報、融資融券，以及 TPEx 逐股三大法人。月營收與財報以官方 `出表日期/Date` 作為保守公布日；金額由官方「千元」轉為新台幣元。TPEx 融資券及法人具有明確交易日；TWSE `MI_MARGN` OpenAPI 沒有日期欄位，目前以同批最新 TWSE 盤後日推定並標示「需驗證」。目前 TWSE OpenAPI Swagger 沒有上市逐股三大法人買賣明細，因此不自行編造端點。財報的年化 EPS、毛利率、ROE 是由已公布累計數推估，並非公司另行公布的原始欄位。

官方 OpenAPI 僅提供最新快照，系統從啟用日起逐日／逐月累積，不會捏造過去資料。完整歷史還原價、財報更正版本、歷史下市股、真正含息 0050 基準及即時價公開再散布授權仍**需驗證**。舊版 `twquant cli` 可用 FinMind 免費額度做個人研究回補，但其資料不會在公開網站重新散布。

## 離線程式驗證（供維護者）

```bash
python3.11 -m pip install -e .
python3.11 -m unittest discover -s tests -v
python3.11 -m twquant.cli check --code 2330 --date 2020-04-08
```

最後一行需先依 `twquant sync` 導入 2330 歷史資料：核對證交所原始開／高／低／收 **285／285.5／283／285**，成交股數 **38,698,826**。開啟網站後可比對實際最近盤後日及 2330 原始收盤；不要把除權息還原價和原始價混淆。

**回測結果不保證未來獲利。**
