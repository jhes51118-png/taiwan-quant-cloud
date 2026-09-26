# 台股量化實驗室（Python 3.11+、免費雲端網站）

使用者只需在瀏覽器開啟 Streamlit 網址，不必在自己的電腦安裝 Python。網站由 Streamlit Community Cloud 執行；GitHub Actions 平日台灣時間 22:17 保存 TWSE/TPEx 官方盤後 OpenAPI 最新快照。公開網站只讀取官方來源的行情；FinMind 與 yfinance 個人研究資料不展示於公開網站。

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
3. 進入 [Streamlit Community Cloud](https://share.streamlit.io/)，用自己的 GitHub 登入，選擇此儲存庫、分支 `main`、入口 `app.py`，Python 3.12，按 Deploy。公開網址可分享給別人。
4. 可選在儲存庫 Settings → Secrets and variables → Actions 新增 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_CHAT_ID`；沒有這兩個值時，網站照常更新但不推播。不要把機密寫到檔案。

Streamlit Community Cloud 容器的本機檔案不是永久資料庫；只有 GitHub Actions **提交回儲存庫**的官方快照能跨重啟保存。GitHub 排程可能延後／停用，網站有最近交易日標示。GitHub 儲存庫及網站公開可見，不適合個人交易紀錄與券商憑證。

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

**資料層現況**：舊版 `twquant cli` 還可抓 FinMind 免費逐股資料，含月營收、財報、法人、融資券、除權息及增量更新；公開網站只使用官方盤後價。官方收入/財報批次資料的欄位與實際公布日歸一化、完整歷史下市股、真正含息 0050 基準和即時價公開授權仍**需驗證**。時間欄位 `published_at` 沒有核實時保留 `NULL`，策略會排除這些資料；月營收 `create_time` 不當公告日。官方最新快照的公布日以首次成功取得日保守記錄。

## 離線程式驗證（供維護者）

```bash
python3.11 -m pip install -e .
python3.11 -m unittest discover -s tests -v
python3.11 -m twquant.cli check --code 2330 --date 2020-04-08
```

最後一行需先依 `twquant sync` 導入 2330 歷史資料：核對證交所原始開／高／低／收 **285／285.5／283／285**，成交股數 **38,698,826**。開啟網站後可比對實際最近盤後日及 2330 原始收盤；不要把除權息還原價和原始價混淆。

**回測結果不保證未來獲利。**
