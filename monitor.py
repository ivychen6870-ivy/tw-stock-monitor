# -*- coding: utf-8 -*-
"""
monitor.py（新版）

用 tw_stock_indicators.py（技術指標/訊號/價位/回測）+ fetch_tw_stock_data.py（FinMind資料源）
取代舊版 monitor.py 自己寫的指標計算邏輯。

歷史資料採用「累積式」存法（docs/data/history.json），設計理念沿用你原本的架構：
    - 第一次使用請先跑 backfill_history.py，一次性回補長期歷史（預設3年）
    - 之後 monitor.py 每天只抓最近幾天的新資料疊上去、裁掉最舊的，
      不用每天重新抓一整年份的資料，省時間也省 FinMind API 額度

本次搬遷範圍（已跟你確認過）：
    有搬：核心自選股清單、技術指標訊號判定、進場/停損/停利價位、LINE+Telegram推播、
          docs/data/ 輸出（給網頁看板用）
    先不搬：動態觀察清單、GitHub Issue關注股票請求、投資論點追蹤、催化事件追蹤
          （這些之後有需要再加回來）

【重要】這個檔案沒辦法在 Claude 這邊執行測試，因為這裡的網路權限沒有開放給
股市資料網站，請放進 GitHub Actions repo 後實際跑一次確認。

環境變數（GitHub Secrets）需要準備：
    LINE_CHANNEL_ACCESS_TOKEN, LINE_TARGET_ID（沿用你原本的）
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID（沿用你原本的）
    FINMIND_TOKEN（新增，沒有的話免費額度是300次/小時，建議還是去申請一個）
    SKIP_PUSH（測試模式開關，沿用你原本的用法）

安裝需求：pip install requests pandas
"""

import os
import json
from datetime import datetime, timedelta

import pandas as pd
import requests

from fetch_tw_stock_data import fetch_futures_daily
from fetch_tw_price_native import (
    fetch_stock_price_range_adjusted, fetch_index_price_range,
    fetch_all_market_daily, fetch_otc_market_daily,
)
from tw_stock_indicators import generate_signals, calculate_price_levels, backtest_signal_returns

# ============================================================
# 設定區
# ============================================================

CORE_WATCHLIST = [
    "2330", "2317", "2454", "0050",
    "5309",  # 系統電（上櫃）
    "2421",  # 建準
    "2486",  # 一詮
    "2399",  # 映泰
    "3481",  # 群創
    "3324",  # 雙鴻
    "3017",  # 奇鋐
    "3653",  # 健策
    "8210",  # 勤誠
    "6691",  # 洋基工程
    "5347",  # 世界先進（上櫃）
    "6719",  # 力智
    "3711",  # 日月光投控
    "2344",  # 華邦電
    "2059",  # 川湖
    "6139",  # 亞翔
]

STOCK_NAMES = {
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "0050": "元大台灣50",
    "5309": "系統電", "2421": "建準", "2486": "一詮", "2399": "映泰",
    "3481": "群創", "3324": "雙鴻", "3017": "奇鋐", "3653": "健策",
    "8210": "勤誠", "6691": "洋基工程", "5347": "世界先進", "6719": "力智",
    "3711": "日月光投控", "2344": "華邦電", "2059": "川湖", "6139": "亞翔",
}

FUTURES_ID = "TX"

# 推薦股（全市場掃描）設定
MARKET_SCAN_CANDIDATES = 50   # 第一階段粗篩：當日漲跌幅最大的前N檔進入候選
RECOMMEND_TOTAL = 30          # 第二階段精算後，最終推薦股數量
CANDIDATE_LOOKBACK_DAYS = 130 # 候選股只抓約4.5個月歷史（夠算大部分指標，MA120/240會是NaN，這是預期取捨）

DATA_DIR = "docs/data"
HISTORY_FILE_NAME = "history.json"

BACKFILL_LOOKBACK_DAYS = 1100     # 第一次使用/history.json裡沒有這個標的時，回補約3年資料
DAILY_FETCH_LOOKBACK_DAYS = 10    # 平常每天只抓最近10天疊上去，避免重複抓一整年
MAX_HISTORY_RECORDS = 800         # 保留上限（約3年多的交易日），避免history.json無限長大

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TARGET_ID = os.environ.get("LINE_TARGET_ID", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SKIP_PUSH = os.environ.get("SKIP_PUSH", "false").lower() == "true"

DIVIDER = "━━━━━━━━━━━━━━"


# ============================================================
# 歷史資料存取（docs/data/history.json）
# ============================================================

def load_history() -> dict:
    path = os.path.join(DATA_DIR, HISTORY_FILE_NAME)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"讀取 {path} 失敗，視為空歷史重新開始：{e}")
    return {}


def save_history(history: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, HISTORY_FILE_NAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)


def df_to_records(df: pd.DataFrame) -> list:
    return [
        {
            "date": str(idx.date()),
            "open": round(float(r.open), 2),
            "high": round(float(r.high), 2),
            "low": round(float(r.low), 2),
            "close": round(float(r.close), 2),
            "volume": round(float(r.volume), 0) if "volume" in df.columns else 0,
        }
        for idx, r in df.iterrows()
    ]


def records_to_df(records: list) -> pd.DataFrame:
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()[["open", "high", "low", "close", "volume"]]


def merge_and_trim(existing_records: list, new_df: pd.DataFrame, max_records: int = MAX_HISTORY_RECORDS) -> list:
    """
    把新抓到的資料疊上既有歷史，同一天的資料以新抓到的為準（處理當天資料事後修正的情況），
    依日期排序後只保留最近 max_records 筆，避免 history.json 隨時間無限長大。
    """
    merged = {r["date"]: r for r in existing_records}
    for r in df_to_records(new_df):
        merged[r["date"]] = r
    sorted_records = [merged[d] for d in sorted(merged.keys())]
    return sorted_records[-max_records:]


def get_symbol_dataframe(history: dict, key: str, fetch_fn) -> pd.DataFrame:
    """
    取得單一標的的完整DataFrame：
    - history.json 裡已經有資料 -> 只抓最近 DAILY_FETCH_LOOKBACK_DAYS 天疊上去（快、省額度）
    - history.json 裡還沒有這個標的（第一次追蹤/忘了跑backfill）-> 自動回補約3年資料，
      不會讓程式失敗，只是這次執行會比平常慢一點
    fetch_fn(start_date) -> DataFrame，由呼叫端決定要抓股票/大盤/期貨哪一種
    """
    existing = history.get(key, [])
    if existing:
        start_date = (datetime.now() - timedelta(days=DAILY_FETCH_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    else:
        print(f"  {key} 在 history.json 裡沒有資料，自動回補約{BACKFILL_LOOKBACK_DAYS}天（建議之後改跑 backfill_history.py 較快）")
        start_date = (datetime.now() - timedelta(days=BACKFILL_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    new_df = fetch_fn(start_date)
    merged_records = merge_and_trim(existing, new_df)
    history[key] = merged_records
    return records_to_df(merged_records)


# ============================================================
# 推播（沿用你原本 LINE 優先、失敗改 Telegram 備援的邏輯）
# ============================================================

def send_line_message(text: str) -> bool:
    import requests
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_TARGET_ID:
        print("LINE推播略過：LINE_CHANNEL_ACCESS_TOKEN 或 LINE_TARGET_ID 未設定")
        return False
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    payload = {"to": LINE_TARGET_ID, "messages": [{"type": "text", "text": text}]}
    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
    if resp.status_code != 200:
        print(f"LINE推播失敗，status={resp.status_code}，回應內容：{resp.text}")
    return resp.status_code == 200


def send_telegram_message(text: str) -> bool:
    import requests
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    return resp.status_code == 200


def push_message(text: str):
    if SKIP_PUSH:
        print("【測試模式，未實際推播】\n" + text)
        return
    if send_line_message(text):
        print("LINE推播成功")
        return
    print("LINE推播失敗，改嘗試Telegram")
    if send_telegram_message(text):
        print("Telegram推播成功")
    else:
        print("Telegram推播也失敗（或未設定），這則訊息沒有送出")


# ============================================================
# 單一標的：算訊號 → 算價位 → 算回測
# ============================================================

def _dir_of(condition) -> str:
    return "up" if condition else "neutral"


def build_indicator_breakdown(latest) -> list:
    """
    把 tw_stock_indicators.generate_signals() 算出來的欄位，整理成看板要的指標清單。
    對應計分邏輯裡的7個核心指標：MACD、RSI、KD、MA60、BIAS、布林通道、DMI/ADX
    （之前的版本漏掉了BIAS跟布林通道兩項顯示，計分本身沒有漏，只是畫面沒顯示出來）
    """
    def sign_dir(v):
        if v is None or pd.isna(v):
            return "neutral"
        return "up" if v > 0 else ("down" if v < 0 else "neutral")

    close = latest["close"]
    boll_upper = latest.get("boll_upper")
    boll_lower = latest.get("boll_lower")
    bias = latest.get("bias")

    if boll_upper is not None and close >= boll_upper:
        boll_val, boll_dir = "觸及上軌", "down"
    elif boll_lower is not None and close <= boll_lower:
        boll_val, boll_dir = "觸及下軌", "up"
    else:
        boll_val, boll_dir = "區間內", "neutral"

    if bias is not None and bias < -5:
        bias_dir = "up"
    elif bias is not None and bias > 5:
        bias_dir = "down"
    else:
        bias_dir = "neutral"

    items = [
        {"name": "MACD", "value": f"{latest['macd_hist']:.2f}", "dir": sign_dir(latest.get("macd_hist"))},
        {"name": "RSI", "value": f"{latest['rsi']:.1f}", "dir": "up" if latest.get("rsi", 50) < 30 else ("down" if latest.get("rsi", 50) > 70 else "neutral")},
        {"name": "KD", "value": f"K{latest['k']:.0f}/D{latest['d']:.0f}", "dir": "up" if latest.get("k", 0) > latest.get("d", 0) else "down"},
        {"name": "MA60", "value": "站上" if close > latest.get("ma60", 0) else "跌破", "dir": "up" if close > latest.get("ma60", 0) else "down"},
        {"name": "BIAS", "value": f"{bias:.1f}%" if bias is not None else "—", "dir": bias_dir},
        {"name": "布林通道", "value": boll_val, "dir": boll_dir},
        {"name": "DMI/ADX", "value": f"ADX{latest['adx']:.0f}", "dir": "up" if latest.get("plus_di", 0) > latest.get("minus_di", 0) else "down"},
    ]
    return items


def build_pattern_hits(latest) -> list:
    """把當天命中的型態訊號（布林值欄位）整理成看板要的徽章清單"""
    pattern_map = [
        ("ma_golden_cross", "MA黃金交叉", "up"), ("ma_death_cross", "MA死亡交叉", "down"),
        ("kd_golden_cross", "KD黃金交叉", "up"), ("kd_death_cross", "KD死亡交叉", "down"),
        ("breakout_up", "突破近20日壓力", "up"), ("breakout_down", "跌破近20日支撐", "down"),
        ("hammer", "錘子線", "up"), ("hanging_man", "吊人線", "down"),
        ("bullish_engulfing", "多頭吞噬", "up"), ("bearish_engulfing", "空頭吞噬", "down"),
        ("morning_star", "晨星", "up"), ("evening_star", "暮星", "down"),
        ("double_top", "M頭/雙重頂", "down"), ("double_bottom", "W底/雙重底", "up"),
    ]
    hits = [{"label": label, "type": t} for col, label, t in pattern_map if latest.get(col)]
    if latest.get("divergence_rsi") == "bullish":
        hits.append({"label": "RSI底背離", "type": "up"})
    elif latest.get("divergence_rsi") == "bearish":
        hits.append({"label": "RSI頂背離", "type": "down"})
    if latest.get("whipsaw"):
        hits.append({"label": "近期訊號反覆，可信度較低", "type": "info"})
    if not hits:
        hits.append({"label": "今日無特殊型態訊號", "type": "info"})
    return hits


def analyze_symbol(label: str, df: pd.DataFrame, name: str = None) -> dict:
    """
    對單一標的（股票/大盤/期貨）跑完整分析流程，回傳字典結果，
    同時供推播訊息跟網頁JSON使用。
    name：中文名稱（給網頁看板顯示用），沒有提供就沿用 label。
    """
    result = generate_signals(df)
    levels = calculate_price_levels(df, result)
    combined = pd.concat([result, levels], axis=1)

    latest = combined.iloc[-1]
    decision = latest["decision"]

    close = float(latest["close"])
    prev_close = float(combined["close"].iloc[-2]) if len(combined) >= 2 else None
    price_change = round(close - prev_close, 2) if prev_close else 0.0
    change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0

    entry = {
        "label": label,
        "name": name or label,
        "date": str(combined.index[-1].date()),
        "close": round(close, 2),
        "price_change": price_change,
        "change_pct": change_pct,
        "score": round(float(latest["score"]), 2),
        "decision": decision,
        "indicators": build_indicator_breakdown(latest),
        "patterns": build_pattern_hits(latest),
    }

    if decision != "觀望":
        entry["levels"] = {
            "entry_price": round(float(latest["entry_price"]), 2),
            "stop_loss_atr": round(float(latest["stop_loss_atr"]), 2),
            "stop_loss_support": round(float(latest["stop_loss_support"]), 2),
            "take_profit_rr": round(float(latest["take_profit_rr"]), 2),
            "take_profit_resistance": round(float(latest["take_profit_resistance"]), 2),
        }
        # 回測統計比較花時間（要掃整段歷史），資料量不夠長（例如剛開始追蹤的股票）時
        # backtest_signal_returns 會回傳 count=0，不會報錯，屬正常現象
        try:
            bt = backtest_signal_returns(df, result, horizons=(63, 126))
            entry["backtest"] = {
                "h63": {k: v for k, v in bt[63][decision].items() if k != "details"},
                "h126": {k: v for k, v in bt[126][decision].items() if k != "details"},
            }
        except Exception as e:
            print(f"  {label} 回測計算失敗，略過：{e}")

    # 給網頁K線圖用的歷史資料（最近300筆）
    entry["ohlc_history"] = [
        {"date": str(idx.date()), "open": round(float(r.open), 2), "high": round(float(r.high), 2),
         "low": round(float(r.low), 2), "close": round(float(r.close), 2)}
        for idx, r in df.tail(300).iterrows()
    ]

    return entry


# ============================================================
# 推薦股：全市場掃描 + 兩階段篩選
# ============================================================

def is_etf_code(code: str) -> bool:
    """台股ETF代號幾乎都是「00」開頭（0050、00878等），排除掉避免推薦股清單被ETF洗版"""
    return str(code).strip().startswith("00")


def scan_market_candidates(top_n: int = MARKET_SCAN_CANDIDATES) -> tuple:
    """
    第一階段粗篩：一次抓全市場（上市+上櫃）當日資料，用「當日漲跌幅絕對值」排序，
    取前 top_n 檔當候選股。這只是粗篩，不是技術指標評分，純粹用來縮小範圍，
    避免對全部近2000檔股票都做完整技術分析（會超時、也會加重被擋的風險）。

    回傳 (候選股代號清單, {代號: 名稱} 對照表)——名稱直接從全市場資料裡的
    「證券名稱」欄位取得，不需要額外查詢，這樣候選股也能顯示正確中文名稱，
    不會只有代號。
    """
    try:
        market_df = fetch_all_market_daily()
    except Exception as e:
        print(f"全市場（上市）資料抓取失敗，推薦股本次僅能用自選股結果：{e}")
        return [], {}

    try:
        otc_df = fetch_otc_market_daily()
        if not otc_df.empty:
            market_df = pd.concat([market_df, otc_df], ignore_index=True)
    except Exception as e:
        print(f"全市場（上櫃）資料合併失敗，僅使用上市資料：{e}")

    if market_df.empty or "收盤價" not in market_df.columns:
        return [], {}

    pool = market_df[~market_df["證券代號"].astype(str).apply(is_etf_code)].copy()
    pool["漲跌幅%"] = (pool["漲跌價差"] / (pool["收盤價"] - pool["漲跌價差"])) * 100
    pool = pool.dropna(subset=["漲跌幅%"])
    pool = pool.reindex(pool["漲跌幅%"].abs().sort_values(ascending=False).index)

    top_pool = pool.head(top_n)
    ids = top_pool["證券代號"].astype(str).tolist()
    name_lookup = dict(zip(top_pool["證券代號"].astype(str), top_pool["證券名稱"].astype(str))) \
        if "證券名稱" in top_pool.columns else {}

    return ids, name_lookup


def analyze_candidate(stock_id: str, name: str = None) -> dict:
    """
    對候選股做「輕量版」分析：只抓約 CANDIDATE_LOOKBACK_DAYS 天歷史（不是完整3年），
    跑一樣的 generate_signals/calculate_price_levels，但不算回測（歷史太短，回測沒意義），
    也不會寫進 history.json（候選股每天會變動，不是要長期追蹤的固定清單）。
    name：中文名稱，優先用呼叫端傳入的（來自全市場掃描時順便抓到的證券名稱），
    沒有的話退而求其次查 STOCK_NAMES，都沒有就顯示代號本身。
    """
    start_date = (datetime.now() - timedelta(days=CANDIDATE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    df = fetch_stock_price_range_adjusted(stock_id, start_date)

    result = generate_signals(df)
    levels = calculate_price_levels(df, result)
    combined = pd.concat([result, levels], axis=1)
    latest = combined.iloc[-1]
    decision = latest["decision"]

    close = float(latest["close"])
    prev_close = float(combined["close"].iloc[-2]) if len(combined) >= 2 else None
    price_change = round(close - prev_close, 2) if prev_close else 0.0
    change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0

    entry = {
        "label": stock_id,
        "name": name or STOCK_NAMES.get(stock_id, stock_id),
        "date": str(combined.index[-1].date()),
        "close": round(close, 2),
        "price_change": price_change,
        "change_pct": change_pct,
        "score": round(float(latest["score"]), 2),
        "decision": decision,
        "indicators": build_indicator_breakdown(latest),
        "patterns": build_pattern_hits(latest),
        "is_candidate": True,  # 標記這是輕量掃描的候選股，不是完整追蹤的自選股
    }
    if decision != "觀望":
        entry["levels"] = {
            "entry_price": round(float(latest["entry_price"]), 2),
            "stop_loss_atr": round(float(latest["stop_loss_atr"]), 2),
            "stop_loss_support": round(float(latest["stop_loss_support"]), 2),
            "take_profit_rr": round(float(latest["take_profit_rr"]), 2),
            "take_profit_resistance": round(float(latest["take_profit_resistance"]), 2),
        }
    entry["ohlc_history"] = [
        {"date": str(idx.date()), "open": round(float(r.open), 2), "high": round(float(r.high), 2),
         "low": round(float(r.low), 2), "close": round(float(r.close), 2)}
        for idx, r in df.tail(CANDIDATE_LOOKBACK_DAYS).iterrows()
    ]
    return entry


def build_recommend_list(watchlist_results: dict) -> tuple:
    """
    推薦股邏輯：候選股（全市場粗篩後的50檔）+ 自選股本身，兩邊合併，
    依分數絕對值排序，取前 RECOMMEND_TOTAL 檔。
    回傳 (推薦股id清單, 候選股分析結果dict)，候選股結果要併入 latest.json 才能讓網頁查得到。
    """
    candidate_ids, name_lookup = scan_market_candidates()
    candidate_results = {}

    for stock_id in candidate_ids:
        if stock_id in watchlist_results:
            continue  # 已經在自選股裡分析過了，不用重複抓
        try:
            candidate_results[stock_id] = analyze_candidate(stock_id, name=name_lookup.get(stock_id))
        except Exception as e:
            print(f"候選股 {stock_id} 分析失敗，略過：{e}")

    pool = {**watchlist_results, **candidate_results}
    ranked = sorted(pool.items(), key=lambda kv: abs(kv[1].get("score", 0)), reverse=True)
    recommend_ids = [stock_id for stock_id, _ in ranked[:RECOMMEND_TOTAL]]

    return recommend_ids, candidate_results


# ============================================================
# 主流程
# ============================================================

def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    history = load_history()

    all_results = {}
    push_lines = [f"📊 台股每日監控　{datetime.now().strftime('%Y-%m-%d')}", DIVIDER]

    # --- 大盤指數 ---
    try:
        df = get_symbol_dataframe(history, "TAIEX",
                                   lambda sd: fetch_index_price_range(sd))
        all_results["TAIEX"] = analyze_symbol("加權指數", df)
    except Exception as e:
        print(f"大盤指數抓取/分析失敗，略過：{e}")

    # --- 台指期（日盤）---
    try:
        df = get_symbol_dataframe(history, "TX_day",
                                   lambda sd: fetch_futures_daily(FUTURES_ID, sd, token=FINMIND_TOKEN, session="day"))
        all_results["TX_day"] = analyze_symbol("台指期(日盤)", df)
    except Exception as e:
        print(f"台指期日盤抓取/分析失敗，略過：{e}")

    # --- 台指期（夜盤）---
    try:
        df = get_symbol_dataframe(history, "TX_night",
                                   lambda sd: fetch_futures_daily(FUTURES_ID, sd, token=FINMIND_TOKEN, session="night"))
        all_results["TX_night"] = analyze_symbol("台指期(夜盤)", df)
    except Exception as e:
        print(f"台指期夜盤抓取/分析失敗，略過：{e}")

    # --- 核心自選股 ---
    buy_list, sell_list = [], []
    for stock_id in CORE_WATCHLIST:
        try:
            df = get_symbol_dataframe(history, stock_id,
                                       lambda sd, sid=stock_id: fetch_stock_price_range_adjusted(sid, sd))
            entry = analyze_symbol(stock_id, df, name=STOCK_NAMES.get(stock_id, stock_id))
            all_results[stock_id] = entry
            if entry["decision"] == "買進":
                buy_list.append(entry)
            elif entry["decision"] == "賣出":
                sell_list.append(entry)
        except Exception as e:
            print(f"{stock_id} 抓取/分析失敗，略過：{e}")

    # --- 存回累積歷史 ---
    save_history(history)

    # --- 推薦股：全市場掃描 + 自選股，取分數絕對值最大的前30檔 ---
    try:
        watchlist_only = {k: v for k, v in all_results.items() if k in CORE_WATCHLIST}
        recommend_ids, candidate_results = build_recommend_list(watchlist_only)
        all_results.update(candidate_results)
        all_results["_recommend"] = recommend_ids
        print(f"推薦股掃描完成，共 {len(recommend_ids)} 檔（候選股新增分析 {len(candidate_results)} 檔）")
    except Exception as e:
        print(f"推薦股掃描失敗，略過本次推薦更新：{e}")

    # --- 組推播訊息 ---
    def format_entry_line(e):
        lv = e.get("levels", {})
        line = f"{'🔴' if e['decision']=='買進' else '🟢'} {e['label']}　{e['close']}　評分{e['score']}"
        if lv:
            line += (f"\n　　進場 {lv['entry_price']} ／ 停損(ATR) {lv['stop_loss_atr']} "
                     f"／ 停利(RR) {lv['take_profit_rr']}")
        bt = e.get("backtest", {}).get("h63")
        if bt and bt.get("count", 0) > 0:
            line += f"\n　　歷史3個月後：平均{bt['mean_return_pct']}%／勝率{bt['win_rate_pct']}%（樣本{bt['count']}次）"
        return line

    push_lines.append("【核心自選股：買進訊號】")
    push_lines += [format_entry_line(e) for e in buy_list] or ["（無）"]
    push_lines.append("")
    push_lines.append("【核心自選股：賣出訊號】")
    push_lines += [format_entry_line(e) for e in sell_list] or ["（無）"]
    push_lines.append("")
    push_lines.append(DIVIDER)
    push_lines.append("※ 以上訊號僅供參考，不構成投資建議")

    message = "\n".join(push_lines)
    print(message)
    push_message(message)

    # --- 存檔給網頁看板用（當下快照，含判定/評分/價位/回測/K線資料） ---
    all_results["_meta"] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "daily",
    }
    with open(os.path.join(DATA_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False)

    print(f"完成，共分析 {len(all_results)} 個標的，已寫入 {DATA_DIR}/latest.json 與 {DATA_DIR}/{HISTORY_FILE_NAME}")


# ============================================================
# 盤中即時更新
# ============================================================

def fetch_realtime_quotes(stock_ids: list) -> dict:
    """
    抓取盤中即時報價（免費，證交所基本市況報導網站，非官方文件化端點，
    穩定性不像 OpenAPI 有保障，失敗會回傳空字典，呼叫端要自行處理）。
    只查傳入的股票代號，不涵蓋全市場。

    回傳 { 代號: {"name":..., "price":..., "open":..., "high":..., "low":..., "volume":...} }
    """
    if not stock_ids:
        return {}
    query = "|".join(f"tse_{c}.tw" for c in stock_ids)
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={query}&json=1&delay=0"
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"抓取盤中即時報價失敗：{e}")
        return {}

    quotes = {}
    for item in data.get("msgArray", []):
        code = item.get("c")
        try:
            price = float(item.get("z"))
        except (TypeError, ValueError):
            continue  # 尚未成交或非交易時段，沒有最新成交價，這檔這次先跳過
        def safe_float(key, default):
            try:
                return float(item.get(key))
            except (TypeError, ValueError):
                return default
        quotes[code] = {
            "name": item.get("n", ""),
            "price": price,
            "open": safe_float("o", price),
            "high": safe_float("h", price),
            "low": safe_float("l", price),
            "volume": safe_float("v", 0),
        }
    return quotes


def intraday_main():
    """
    盤中即時更新：用「歷史資料 + 即時報價組成的暫定今日K棒」，完整重跑一次
    generate_signals/calculate_price_levels/backtest_signal_returns，
    效果等同提早看到「如果現在收盤」的技術面判斷。

    這些是暫定訊號，收盤前價格還可能變動，跟收盤後 daily_monitor 正式跑出來的
    結果不一定一樣。只更新 docs/data/latest.json（給網頁看板用），
    不會寫進 docs/data/history.json，避免暫定資料污染正式的歷史紀錄。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    history = load_history()
    if not history:
        print("history.json 是空的，請先跑過 backfill_history.py 或至少一次 daily_monitor，略過本次盤中更新")
        return

    quotes = fetch_realtime_quotes(CORE_WATCHLIST)
    if not quotes:
        print("盤中即時報價抓取失敗或無資料（可能非交易時段），略過本次更新")
        return

    latest_path = os.path.join(DATA_DIR, "latest.json")
    all_results = {}
    if os.path.exists(latest_path):
        try:
            with open(latest_path, "r", encoding="utf-8") as f:
                all_results = json.load(f)
        except Exception as e:
            print(f"讀取既有 latest.json 失敗，將視為空重新開始：{e}")

    today_str = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%H:%M")
    buy_list, sell_list = [], []

    for stock_id in CORE_WATCHLIST:
        q = quotes.get(stock_id)
        hist_records = history.get(stock_id, [])
        if not q or not hist_records:
            continue
        try:
            base_records = [r for r in hist_records if r["date"] != today_str]
            provisional = base_records + [{
                "date": today_str, "open": q["open"], "high": q["high"],
                "low": q["low"], "close": q["price"], "volume": q["volume"],
            }]
            df = records_to_df(provisional)
            entry = analyze_symbol(stock_id, df, name=STOCK_NAMES.get(stock_id, stock_id))
            entry["is_intraday"] = True
            entry["intraday_time"] = now_str
            all_results[stock_id] = entry

            if entry["decision"] == "買進":
                buy_list.append(entry)
            elif entry["decision"] == "賣出":
                sell_list.append(entry)
        except Exception as e:
            print(f"{stock_id} 盤中分析失敗，略過：{e}")

    all_results["_meta"] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "intraday",
    }
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False)

    def format_entry_line(e):
        lv = e.get("levels", {})
        line = f"{'🔴' if e['decision']=='買進' else '🟢'} {e['label']}　{e['close']}　評分{e['score']}"
        if lv:
            line += (f"\n　　進場 {lv['entry_price']} ／ 停損(ATR) {lv['stop_loss_atr']} "
                     f"／ 停利(RR) {lv['take_profit_rr']}")
        return line

    if buy_list or sell_list:
        lines = [f"⏱ 盤中即時訊號　{now_str}", DIVIDER]
        if buy_list:
            lines.append("【盤中買進訊號】")
            lines += [format_entry_line(e) for e in buy_list]
        if sell_list:
            lines.append("【盤中賣出訊號】")
            lines += [format_entry_line(e) for e in sell_list]
        lines.append("")
        lines.append(DIVIDER)
        lines.append("※ 盤中訊號為暫定值，以收盤後正式結果為準，僅供參考，不構成投資建議")
        message = "\n".join(lines)
        print(message)
        push_message(message)
    else:
        print(f"盤中更新完成（{now_str}），目前沒有買進/賣出訊號")

    print(f"盤中分析完成，共更新 {len(quotes)} 檔即時報價")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "intraday":
        intraday_main()
    else:
        main()
