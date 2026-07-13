# -*- coding: utf-8 -*-
"""
backfill_history.py（新版）

一次性用 FinMind 回補長期歷史資料（預設約3年），寫進 docs/data/history.json。
之後 monitor.py 每天執行只會抓最近幾天疊上去，不用重複抓這一大包。

執行方式：
    python backfill_history.py

【重要】這個檔案沒辦法在 Claude 這邊執行測試，請放進 GitHub Actions repo
（或本機，設定好 FINMIND_TOKEN 環境變數）後實際跑一次。

跟舊版 backfill_history.py 的差異：
    - 資料源從「證交所/櫃買直接爬蟲」改成 FinMind（含還原股價、期貨）
    - 不用另外處理除權息還原，FinMind 的 TaiwanStockPriceAdj 已經是還原股價
    - 額外回補了大盤指數、台指期（日盤+夜盤），舊版沒有這兩個

注意事項：
    - 這是「一次性」工具，不是每天排程的東西，第一次建置或需要重新拉長歷史時才跑
    - 如果 CORE_WATCHLIST 裡的股票數量多，跑完可能要幾分鐘，屬正常現象
    - FinMind 免費額度 300次/小時（有token 600次/小時），股票+大盤+期貨×2 加起來
      通常不會超過額度，但如果中途出現額度限制的錯誤訊息，等一小時後重跑即可
"""

import os
import time
from datetime import datetime, timedelta

from fetch_tw_stock_data import fetch_price_data, fetch_futures_daily, fetch_index_price
from monitor import (
    CORE_WATCHLIST, TAIEX_DATA_ID, FUTURES_ID, DATA_DIR, HISTORY_FILE_NAME,
    FINMIND_TOKEN, BACKFILL_LOOKBACK_DAYS, MAX_HISTORY_RECORDS,
    load_history, save_history, df_to_records,
)


def backfill_one(history: dict, key: str, label: str, fetch_fn, start_date: str):
    print(f"回補 {label}（{key}）...")
    try:
        df = fetch_fn(start_date)
        records = df_to_records(df)[-MAX_HISTORY_RECORDS:]
        history[key] = records
        print(f"  完成，共 {len(records)} 筆（{records[0]['date']} ~ {records[-1]['date']}）")
    except Exception as e:
        print(f"  失敗，略過：{e}")
    time.sleep(0.3)  # 避免請求過快被限流


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    start_date = (datetime.now() - timedelta(days=BACKFILL_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    history = load_history()

    backfill_one(history, "TAIEX", "加權指數",
                 lambda sd: fetch_index_price(TAIEX_DATA_ID, sd, token=FINMIND_TOKEN), start_date)

    backfill_one(history, "TX_day", "台指期(日盤)",
                 lambda sd: fetch_futures_daily(FUTURES_ID, sd, token=FINMIND_TOKEN, session="day"), start_date)

    backfill_one(history, "TX_night", "台指期(夜盤)",
                 lambda sd: fetch_futures_daily(FUTURES_ID, sd, token=FINMIND_TOKEN, session="night"), start_date)

    for stock_id in CORE_WATCHLIST:
        backfill_one(history, stock_id, stock_id,
                     lambda sd, sid=stock_id: fetch_price_data(sid, sd, token=FINMIND_TOKEN), start_date)

    save_history(history)
    path = os.path.join(DATA_DIR, HISTORY_FILE_NAME)
    print(f"\n回補完成，已寫入 {path}（共 {len(history)} 個標的）")


if __name__ == "__main__":
    main()
