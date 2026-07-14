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

from fetch_tw_stock_data import fetch_futures_daily
from fetch_tw_price_native import fetch_stock_price_range_adjusted, fetch_index_price_range
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
    """把 tw_stock_indicators.generate_signals() 算出來的欄位，整理成看板要的簡短指標清單"""
    def sign_dir(v):
        if v is None or pd.isna(v):
            return "neutral"
        return "up" if v > 0 else ("down" if v < 0 else "neutral")

    items = [
        {"name": "MACD", "value": f"{latest['macd_hist']:.2f}", "dir": sign_dir(latest.get("macd_hist"))},
        {"name": "RSI", "value": f"{latest['rsi']:.1f}", "dir": "up" if latest.get("rsi", 50) < 30 else ("down" if latest.get("rsi", 50) > 70 else "neutral")},
        {"name": "KD", "value": f"K{latest['k']:.0f}/D{latest['d']:.0f}", "dir": "up" if latest.get("k", 0) > latest.get("d", 0) else "down"},
        {"name": "MA60", "value": "站上" if latest["close"] > latest.get("ma60", 0) else "跌破", "dir": "up" if latest["close"] > latest.get("ma60", 0) else "down"},
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
    with open(os.path.join(DATA_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False)

    print(f"完成，共分析 {len(all_results)} 個標的，已寫入 {DATA_DIR}/latest.json 與 {DATA_DIR}/{HISTORY_FILE_NAME}")


if __name__ == "__main__":
    main()
