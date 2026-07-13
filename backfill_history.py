# -*- coding: utf-8 -*-
"""
backfill_history.py（新版）

一次性回補長期歷史資料（預設約3年），寫進 docs/data/history.json。
之後 monitor.py 每天執行只會抓最近幾天疊上去，不用重複抓這一大包。

資料來源分工：
    股票/大盤指數：直接爬證交所/櫃買中心（免費），含除權息還原
                  （fetch_tw_price_native.py，port自你原本monitor.py的邏輯）
    期貨（日盤/夜盤）：FinMind（免費資料集，你原本系統沒有這塊）

執行方式：
    python backfill_history.py

【重要】這個檔案沒辦法在 Claude 這邊執行測試，請放進 GitHub Actions repo
（或本機）後實際跑一次。

注意事項：
    - 這是「一次性」工具，不是每天排程的東西
    - 20檔股票 + 大盤指數，每個都要抓約3年（36個月）份的月度資料，
      每次呼叫之間有 sleep(0.3秒) 避免被證交所/櫃買擋掉，跑完可能要幾分鐘，屬正常現象
    - 期貨部分用 FinMind，額度 300次/小時（有token 600次/小時）
"""

import os
import time
from datetime import datetime, timedelta

from fetch_tw_stock_data import fetch_futures_daily
from fetch_tw_price_native import fetch_stock_price_range_adjusted, fetch_index_price_range
from monitor import (
    CORE_WATCHLIST, FUTURES_ID, DATA_DIR, HISTORY_FILE_NAME,
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
    time.sleep(0.3)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    start_date = (datetime.now() - timedelta(days=BACKFILL_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    history = load_history()

    backfill_one(history, "TAIEX", "加權指數",
                 lambda sd: fetch_index_price_range(sd), start_date)

    backfill_one(history, "TX_day", "台指期(日盤)",
                 lambda sd: fetch_futures_daily(FUTURES_ID, sd, token=FINMIND_TOKEN, session="day"), start_date)

    backfill_one(history, "TX_night", "台指期(夜盤)",
                 lambda sd: fetch_futures_daily(FUTURES_ID, sd, token=FINMIND_TOKEN, session="night"), start_date)

    for stock_id in CORE_WATCHLIST:
        backfill_one(history, stock_id, stock_id,
                     lambda sd, sid=stock_id: fetch_stock_price_range_adjusted(sid, sd), start_date)

    save_history(history)
    path = os.path.join(DATA_DIR, HISTORY_FILE_NAME)
    print(f"\n回補完成，已寫入 {path}（共 {len(history)} 個標的）")


if __name__ == "__main__":
    main()
