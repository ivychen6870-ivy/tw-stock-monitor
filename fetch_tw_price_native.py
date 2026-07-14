# -*- coding: utf-8 -*-
"""
fetch_tw_price_native.py

股價/大盤指數資料，改用「直接爬證交所/櫃買中心」的方式抓取，完全免費，
不依賴 FinMind 的還原股價資料集（那個是付費 backer/sponsor 限定）。

這裡的程式碼是從你原本 monitor.py 舊版裡的證交所爬蟲邏輯搬過來的，
包含除權息還原（你原本已經驗證過可以動的邏輯，原封不動保留）。

FinMind 保留給期貨（日盤/夜盤）、三大法人籌碼、月營收/財報這幾塊，
因為那些是免費的，而且是你原本系統沒有的新功能。

【重要】這個檔案沒辦法在 Claude 這邊實際連線測試，請放進 GitHub Actions repo
後實際跑一次確認。

安裝需求：pip install requests pandas
"""

import time
import os
from datetime import datetime, timedelta

import requests
import pandas as pd

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")


# ============================================================
# 個股月度OHLC（上市 STOCK_DAY + 上櫃 st43_result）
# ============================================================

def fetch_month_ohlc(stock_id: str, year: int, month: int) -> list:
    """抓取單一股票、單一月份的完整每日OHLC（上市，證交所 STOCK_DAY）"""
    date_str = f"{year}{month:02d}01"
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
    params = {"response": "json", "date": date_str, "stockNo": stock_id}
    try:
        resp = requests.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  {stock_id} {year}-{month:02d}（上市）抓取失敗：{e}")
        return []

    if data.get("stat") != "OK" or "data" not in data:
        return []

    rows = []
    for row in data["data"]:
        try:
            roc_date = row[0]
            y, m, d = roc_date.split("/")
            date_str_out = f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"
            rows.append({
                "date": date_str_out,
                "open": float(row[3].replace(",", "")),
                "high": float(row[4].replace(",", "")),
                "low": float(row[5].replace(",", "")),
                "close": float(row[6].replace(",", "")),
                "volume": float(row[1].replace(",", "")),
            })
        except (ValueError, IndexError):
            continue
    return rows


def fetch_otc_stock_month_ohlc(stock_id: str, year: int, month: int) -> list:
    """抓取上櫃個股某個月份的完整每日OHLC（櫃買中心 st43_result）"""
    roc_year = year - 1911
    date_str = f"{roc_year}/{month:02d}"
    url = "https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php"
    params = {"d": date_str, "stkno": stock_id}
    try:
        resp = requests.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  {stock_id} {year}-{month:02d}（上櫃）抓取失敗：{e}")
        return []

    rows_raw = data.get("aaData", [])
    rows = []
    for row in rows_raw:
        try:
            roc_date = row[0]
            y, m, d = roc_date.split("/")
            date_str_out = f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"
            rows.append({
                "date": date_str_out,
                "open": float(str(row[3]).replace(",", "")),
                "high": float(str(row[4]).replace(",", "")),
                "low": float(str(row[5]).replace(",", "")),
                "close": float(str(row[6]).replace(",", "")),
                "volume": float(str(row[1]).replace(",", "")),
            })
        except (ValueError, IndexError):
            continue
    return rows


def fetch_otc_month_via_finmind(stock_id: str, year: int, month: int, token: str = "") -> list:
    """
    備援用：櫃買中心 st43_result 端點常被雲端環境（例如 GitHub Actions）的防爬蟲機制擋掉
    （已實測確認，錯誤訊息通常是 JSON 解析失敗或 520 Server Error）。
    這裡改用 FinMind 免費的 TaiwanStockPrice 資料集當備援（涵蓋上市/上櫃/興櫃），
    沒有除權息還原，但至少抓得到資料，不會讓這檔股票完全開天窗。
    """
    import requests as _requests
    start = f"{year}-{month:02d}-01"
    if month == 12:
        end = f"{year+1}-01-01"
    else:
        end = f"{year}-{month+1:02d}-01"

    url = "https://api.finmindtrade.com/api/v4/data"
    params = {"dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start, "end_date": end}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = _requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as e:
        print(f"  {stock_id} {year}-{month:02d}（FinMind備援）也抓取失敗：{e}")
        return []

    rows = []
    for row in data:
        try:
            rows.append({
                "date": row["date"],
                "open": float(row["open"]),
                "high": float(row["max"]),
                "low": float(row["min"]),
                "close": float(row["close"]),
                "volume": float(row["Trading_Volume"]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return rows


# 已經確認過的上櫃股票（櫃買中心端點在 GitHub Actions 雲端環境幾乎必定被擋），
# 這幾檔直接跳過上市/上櫃爬蟲，改走 FinMind 備援，避免每個月都跑一次注定失敗的請求，
# 讓 log 乾淨、也跑得快一點。之後如果 CORE_WATCHLIST 加了新的上櫃股票，
# 先讓它照正常流程跑（上市->上櫃->FinMind），如果穩定失敗再加進這個清單。
KNOWN_OTC_STOCKS_NEEDING_FINMIND = {"5309", "3324", "5347"}


def fetch_month_ohlc_any_market(stock_id: str, year: int, month: int, finmind_token: str = "") -> list:
    """
    先試上市（證交所），抓不到再試上櫃（櫃買中心），
    櫃買中心也失敗的話（常見於雲端環境被擋），最後改用 FinMind 免費資料集當備援。

    stock_id 在 KNOWN_OTC_STOCKS_NEEDING_FINMIND 清單裡的話，直接跳過前兩層爬蟲，
    省下注定失敗的請求時間。
    """
    if stock_id in KNOWN_OTC_STOCKS_NEEDING_FINMIND:
        return fetch_otc_month_via_finmind(stock_id, year, month, finmind_token)

    rows = fetch_month_ohlc(stock_id, year, month)
    if rows:
        return rows

    rows = fetch_otc_stock_month_ohlc(stock_id, year, month)
    if rows:
        return rows

    print(f"  {stock_id} {year}-{month:02d} 上市/上櫃爬蟲都抓不到，改用 FinMind 備援")
    return fetch_otc_month_via_finmind(stock_id, year, month, finmind_token)


def fetch_stock_price_range(stock_id: str, start_date: str, end_date: str = None) -> pd.DataFrame:
    """
    抓取單一股票在 [start_date, end_date] 區間內的每日OHLC，跨月份自動抓齊。
    回傳 index 為日期、欄位為 open/high/low/close/volume 的 DataFrame，
    格式跟 tw_stock_indicators.py 需要的輸入一致。
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()

    months = []
    cur = start.replace(day=1)
    while cur <= end:
        months.append((cur.year, cur.month))
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)

    all_rows = []
    for y, m in months:
        all_rows.extend(fetch_month_ohlc_any_market(stock_id, y, m, finmind_token=FINMIND_TOKEN))
        time.sleep(0.3)

    if not all_rows:
        raise ValueError(f"查無股價資料：stock_id={stock_id}, start_date={start_date}")

    seen = {r["date"]: r for r in all_rows}
    sorted_rows = [seen[d] for d in sorted(seen.keys()) if d >= start_date]

    df = pd.DataFrame(sorted_rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()[["open", "high", "low", "close", "volume"]]


# ============================================================
# 除權息還原
# ============================================================

def fetch_dividend_events(stock_id: str, start_date: str, end_date: str) -> list:
    """
    抓取指定股票在區間內的除權息事件（免費，證交所TWT49U端點）。
    start_date/end_date 格式：純數字 "YYYYMMDD"
    回傳依日期排序：[{"date":"2026-06-15","cash_dividend":2.5,"stock_dividend_rate":0.0}, ...]
    """
    url = "https://www.twse.com.tw/exchangeReport/TWT49U"
    params = {"response": "json", "strDate": start_date, "endDate": end_date}
    try:
        resp = requests.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"抓取除權息資料失敗（{stock_id}）：{e}")
        return []

    fields = data.get("fields", [])
    rows_raw = data.get("data", [])
    if not fields or not rows_raw:
        return []

    def find_col(*keywords):
        for i, f in enumerate(fields):
            if all(k in f for k in keywords):
                return i
        return None

    idx_date = find_col("日期")
    idx_code = find_col("代號")
    idx_cash = find_col("現金股利")
    idx_stock_rate = find_col("無償配股")

    events = []
    for row in rows_raw:
        try:
            if idx_code is None or row[idx_code].strip() != str(stock_id):
                continue
            roc_date = row[idx_date]
            y, m, d = roc_date.split("/")
            ex_date = f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"
            cash_raw = row[idx_cash].replace(",", "") if idx_cash is not None else ""
            cash = float(cash_raw) if cash_raw not in ("", "-") else 0.0
            rate_raw = row[idx_stock_rate].replace(",", "") if idx_stock_rate is not None else ""
            stock_rate = (float(rate_raw) / 1000) if rate_raw not in ("", "-") else 0.0
            events.append({"date": ex_date, "cash_dividend": cash, "stock_dividend_rate": stock_rate})
        except (ValueError, IndexError, AttributeError, KeyError):
            continue
    return sorted(events, key=lambda e: e["date"])


def adjust_series_for_dividends(series: list, events: list) -> list:
    """
    還原股價（回溯調整法）：對每一次除權息事件，往回把除權息日之前的所有OHLC
    乘上一個調整係數，讓價格連續，不會因為配股配息出現假的跳空缺口。
    """
    if not events:
        return series
    series = [dict(s) for s in series]
    for ev in sorted(events, key=lambda e: e["date"], reverse=True):
        ex_date = ev["date"]
        prior = [s for s in series if s["date"] < ex_date]
        if not prior:
            continue
        prev_close = prior[-1]["close"]
        if prev_close <= 0:
            continue
        ex_price = (prev_close - ev.get("cash_dividend", 0)) / (1 + ev.get("stock_dividend_rate", 0))
        if ex_price <= 0:
            continue
        ratio = ex_price / prev_close
        for s in series:
            if s["date"] < ex_date:
                s["open"] = round(s["open"] * ratio, 2)
                s["high"] = round(s["high"] * ratio, 2)
                s["low"] = round(s["low"] * ratio, 2)
                s["close"] = round(s["close"] * ratio, 2)
    return series


def fetch_stock_price_range_adjusted(stock_id: str, start_date: str, end_date: str = None) -> pd.DataFrame:
    """
    抓股價 + 自動套用除權息還原，這是股票用途應該優先呼叫的函式。
    大盤指數請用 fetch_index_price_range（指數沒有除權息，不用還原）。
    """
    df = fetch_stock_price_range(stock_id, start_date, end_date)

    range_start = df.index.min().strftime("%Y%m%d")
    range_end = df.index.max().strftime("%Y%m%d")
    try:
        events = fetch_dividend_events(stock_id, range_start, range_end)
    except Exception as e:
        print(f"  {stock_id} 除權息事件查詢失敗，使用未還原股價：{e}")
        events = []

    if not events:
        return df

    records = [
        {"date": str(idx.date()), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
        for idx, r in df.iterrows()
    ]
    adjusted = adjust_series_for_dividends(records, events)

    out = pd.DataFrame(adjusted)
    out["date"] = pd.to_datetime(out["date"])
    return out.set_index("date").sort_index()[["open", "high", "low", "close", "volume"]]


# ============================================================
# 大盤指數（發行量加權股價指數）
# ============================================================

def fetch_index_month_ohlc(year: int, month: int) -> list:
    """抓取大盤指數某個月份的完整每日OHLC（證交所 MI_5MINS_HIST）"""
    date_str = f"{year}{month:02d}01"
    url = "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST"
    params = {"response": "json", "date": date_str}
    try:
        resp = requests.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  大盤指數 {year}-{month:02d} 抓取失敗：{e}")
        return []

    rows_raw = data.get("data", [])
    rows = []
    for row in rows_raw:
        try:
            roc_date = row[0]
            y, m, d = roc_date.split("/")
            date_str_out = f"{int(y) + 1911}-{int(m):02d}-{int(d):02d}"
            rows.append({
                "date": date_str_out,
                "open": float(str(row[1]).replace(",", "")),
                "high": float(str(row[2]).replace(",", "")),
                "low": float(str(row[3]).replace(",", "")),
                "close": float(str(row[4]).replace(",", "")),
                "volume": 0,
            })
        except (ValueError, IndexError):
            continue
    return rows


def fetch_index_price_range(start_date: str, end_date: str = None) -> pd.DataFrame:
    """
    抓取大盤指數在 [start_date, end_date] 區間內的每日OHLC，跨月份自動抓齊。
    指數沒有除權息，不需要還原。
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()

    months = []
    cur = start.replace(day=1)
    while cur <= end:
        months.append((cur.year, cur.month))
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)

    all_rows = []
    for y, m in months:
        all_rows.extend(fetch_index_month_ohlc(y, m))
        time.sleep(0.3)

    if not all_rows:
        raise ValueError(f"查無大盤指數資料：start_date={start_date}")

    seen = {r["date"]: r for r in all_rows}
    sorted_rows = [seen[d] for d in sorted(seen.keys()) if d >= start_date]

    df = pd.DataFrame(sorted_rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()[["open", "high", "low", "close", "volume"]]
