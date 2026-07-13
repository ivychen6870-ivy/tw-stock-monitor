# -*- coding: utf-8 -*-
"""
monitor.py（新版）

用 tw_stock_indicators.py（技術指標/訊號/價位/回測）+ fetch_tw_stock_data.py（FinMind資料源）
取代舊版 monitor.py 自己寫的指標計算邏輯。

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

from fetch_tw_stock_data import fetch_price_data, fetch_futures_daily
from tw_stock_indicators import generate_signals, calculate_price_levels, backtest_signal_returns

# ============================================================
# 設定區
# ============================================================

# 核心自選股清單（原封不動沿用你 monitor.py 舊版的清單）
# 注意：5309(系統電)、5347(世界先進) 是上櫃股票，FinMind涵蓋上市櫃興櫃，理論上抓得到，
# 但務必在 GitHub Actions 上第一次跑的時候實際確認這兩檔有沒有正常回傳資料
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

TAIEX_DATA_ID = "001"  # FinMind的加權指數代號（3碼指數代號，不是股票代號）
FUTURES_ID = "TX"

DATA_DIR = "docs/data"
HISTORY_LOOKBACK_DAYS = 500  # 抓500個日曆天，確保有超過240個交易日可以算年線

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TARGET_ID = os.environ.get("LINE_TARGET_ID", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SKIP_PUSH = os.environ.get("SKIP_PUSH", "false").lower() == "true"

DIVIDER = "━━━━━━━━━━━━━━"


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
# 單一標的：抓資料 → 算訊號 → 算價位 → 算回測
# ============================================================

def analyze_symbol(label: str, df: pd.DataFrame) -> dict:
    """
    對單一標的（股票/大盤/期貨）跑完整分析流程，回傳字典結果，
    同時供推播訊息跟網頁JSON使用。
    """
    result = generate_signals(df)
    levels = calculate_price_levels(df, result)
    combined = pd.concat([result, levels], axis=1)

    latest = combined.iloc[-1]
    decision = latest["decision"]

    entry = {
        "label": label,
        "date": str(combined.index[-1].date()),
        "close": round(float(latest["close"]), 2),
        "score": round(float(latest["score"]), 2),
        "decision": decision,
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

    # 給網頁K線圖用的歷史資料（最近300筆，含均線需要的原始OHLC）
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
    start_date = (datetime.now() - timedelta(days=HISTORY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    all_results = {}
    push_lines = [f"📊 台股每日監控　{datetime.now().strftime('%Y-%m-%d')}", DIVIDER]

    # --- 大盤指數 ---
    try:
        taiex_df = fetch_price_data(TAIEX_DATA_ID, start_date, token=FINMIND_TOKEN)
        all_results["TAIEX"] = analyze_symbol("加權指數", taiex_df)
    except Exception as e:
        print(f"大盤指數抓取/分析失敗，略過：{e}")

    # --- 台指期（日盤）---
    try:
        futures_df = fetch_futures_daily(FUTURES_ID, start_date, token=FINMIND_TOKEN, session="day")
        all_results["TX_day"] = analyze_symbol("台指期(日盤)", futures_df)
    except Exception as e:
        print(f"台指期日盤抓取/分析失敗，略過：{e}")

    # --- 台指期（夜盤）---
    try:
        futures_night_df = fetch_futures_daily(FUTURES_ID, start_date, token=FINMIND_TOKEN, session="night")
        all_results["TX_night"] = analyze_symbol("台指期(夜盤)", futures_night_df)
    except Exception as e:
        print(f"台指期夜盤抓取/分析失敗，略過：{e}")

    # --- 核心自選股 ---
    buy_list, sell_list = [], []
    for stock_id in CORE_WATCHLIST:
        try:
            df = fetch_price_data(stock_id, start_date, token=FINMIND_TOKEN)
            entry = analyze_symbol(stock_id, df)
            all_results[stock_id] = entry
            if entry["decision"] == "買進":
                buy_list.append(entry)
            elif entry["decision"] == "賣出":
                sell_list.append(entry)
        except Exception as e:
            print(f"{stock_id} 抓取/分析失敗，略過：{e}")

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

    # --- 存檔給網頁看板用 ---
    with open(os.path.join(DATA_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False)

    print(f"完成，共分析 {len(all_results)} 個標的，已寫入 {DATA_DIR}/latest.json")


if __name__ == "__main__":
    main()
