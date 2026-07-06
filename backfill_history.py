"""
一次性回補歷史資料
用途：從證交所「個股日成交資訊」API，把過去N個月的完整開高低收(OHLC)抓回來，
直接灌進 docs/data/history.json，並算好 MA5/MA20/KD，
這樣不用等系統每天累積一筆，馬上就能有K線圖跟技術指標可以看。

執行方式：
- 本機測試：python backfill_history.py
- 或透過 GitHub Actions 手動觸發（.github/workflows/backfill.yml）

注意：這是「一次性」工具，不是每天排程執行的東西，跑完一次之後
之後 monitor.py 每天的正常執行會接著往後累積，兩者資料是同一份 history.json，互相銜接。
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta

from monitor import (
    CORE_WATCHLIST, DATA_DIR, HISTORY_FILE_NAME, HISTORY_MAX_DAYS,
    compute_ma, compute_kd, fetch_dividend_events, adjust_series_for_dividends,
)

# 要回補幾個月的資料（6個月大約可以讓 MA20、KD、支撐壓力這些指標都有足夠資料可算）
BACKFILL_MONTHS = 6


def fetch_month_ohlc(stock_id: str, year: int, month: int) -> list:
    """
    抓取單一股票、單一月份的完整每日OHLC。
    回傳 [{"date":"2026-01-05","open":...,"high":...,"low":...,"close":...}, ...]
    """
    date_str = f"{year}{month:02d}01"
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
    params = {"response": "json", "date": date_str, "stockNo": stock_id}
    try:
        resp = requests.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  {stock_id} {year}-{month:02d} 抓取失敗：{e}")
        return []

    if data.get("stat") != "OK" or "data" not in data:
        return []

    # fields 通常是：日期,成交股數,成交金額,開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數
    rows = []
    for row in data["data"]:
        try:
            roc_date = row[0]  # 民國年日期，格式如 "115/01/05"
            y, m, d = roc_date.split("/")
            greg_year = int(y) + 1911
            date_str_out = f"{greg_year}-{int(m):02d}-{int(d):02d}"
            rows.append({
                "date": date_str_out,
                "open": float(row[3].replace(",", "")),
                "high": float(row[4].replace(",", "")),
                "low": float(row[5].replace(",", "")),
                "close": float(row[6].replace(",", "")),
                "volume": float(row[1].replace(",", "")),
            })
        except (ValueError, IndexError):
            continue  # 跳過格式異常的列（例如當月除權息造成的特殊列）
    return rows


def backfill_stock(stock_id: str) -> list:
    """回補單一股票過去 BACKFILL_MONTHS 個月的資料，回傳依日期排序的OHLC清單"""
    all_rows = []
    today = datetime.now()
    for i in range(BACKFILL_MONTHS, 0, -1):
        target = today - timedelta(days=30 * i)
        rows = fetch_month_ohlc(stock_id, target.year, target.month)
        all_rows.extend(rows)
        time.sleep(0.5)  # 避免請求太快被證交所擋掉

    # 去重、排序
    seen = {}
    for r in all_rows:
        seen[r["date"]] = r
    sorted_rows = [seen[d] for d in sorted(seen.keys())]
    return sorted_rows[-HISTORY_MAX_DAYS:]


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    history_path = os.path.join(DATA_DIR, HISTORY_FILE_NAME)

    history = {}
    if os.path.exists(history_path):
        with open(history_path, "r", encoding="utf-8") as f:
            history = json.load(f)

    for stock_id in CORE_WATCHLIST:
        print(f"回補 {stock_id} ...")
        rows = backfill_stock(stock_id)
        if not rows:
            print(f"  {stock_id} 沒有抓到資料（可能是上櫃股票，或代號有誤）")
            continue

        # 抓這段期間的除權息事件，做還原股價調整，避免除權息造成假的交叉訊號
        start_date = rows[0]["date"].replace("-", "")
        end_date = rows[-1]["date"].replace("-", "")
        try:
            events = fetch_dividend_events(stock_id, start_date, end_date)
            if events:
                rows = adjust_series_for_dividends(rows, events)
                print(f"  已套用 {len(events)} 筆除權息還原調整")
        except Exception as e:
            print(f"  {stock_id} 除權息調整失敗，使用原始價格：{e}")

        closes = [r["close"] for r in rows]
        highs = [r["high"] for r in rows]
        lows = [r["low"] for r in rows]
        ma5 = compute_ma(closes, 5)
        ma20 = compute_ma(closes, 20)
        k_vals, d_vals = compute_kd(highs, lows, closes)
        for i, r in enumerate(rows):
            r["ma5"] = ma5[i]
            r["ma20"] = ma20[i]
            r["k"] = k_vals[i]
            r["d"] = d_vals[i]

        history[stock_id] = rows
        print(f"  完成，共 {len(rows)} 筆")

    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)

    print("回補完成，已寫入", history_path)


if __name__ == "__main__":
    main()

