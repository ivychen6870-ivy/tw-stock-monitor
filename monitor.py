"""
台股每日監控機器人 - 免費架構版本
資料源：證交所 OpenAPI（免費）
運算：pandas + pandas_ta（開源）
推播：LINE Messaging API（每月200則內）+ Telegram Bot（免費備援）

執行方式：由 GitHub Actions 每日收盤後排程觸發
"""

import os
import json
import requests
import pandas as pd
from datetime import datetime

# ============================================================
# 0. 設定區 —— 之後全部改成你自己的清單/門檻即可
# ============================================================

# 固定層：你手動關注的核心自選股（股票代號）
CORE_WATCHLIST = ["2330", "2317", "2454", "0050"]

# 動態層每個類別最多納入幾檔，避免推播爆量
DYNAMIC_LIMIT_PER_CATEGORY = 10

# 價格到價提醒門檻（範例：單日漲跌幅超過 ±5%）
PRICE_ALERT_PCT = 5.0

# 成交量異常門檻（範例：當日量 > 過去均量的 3 倍，此範例先略過均量計算，
# 實務上需要額外抓歷史資料才能算，這裡先用「當日量排名前N大」示範）
VOLUME_TOP_N = 10

# LINE / Telegram 憑證，從 GitHub Actions 的 Secrets 帶入，不要寫死在程式裡
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TARGET_ID = os.environ.get("LINE_TARGET_ID", "")  # 使用者或群組 ID
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ============================================================
# 1. 資料源層：抓取「全市場」當日資料（一次打包，不用逐檔查）
# ============================================================

def fetch_all_market_daily():
    """
    抓取上市全部個股當日成交資訊（政府開放資料，免費，無需金鑰）
    回傳 DataFrame，欄位包含：證券代號、證券名稱、開盤價、最高價、最低價、
    收盤價、漲跌價差、成交股數 等
    """
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()

    df = pd.read_csv(pd.io.common.StringIO(resp.text))
    # 欄位清理：數值欄位轉成數字（原始資料常帶千分位逗號）
    numeric_cols = ["成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價", "漲跌價差", "成交筆數"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", ""), errors="coerce")
    return df


def fetch_institutional_investors():
    """
    抓取上市三大法人買賣超（免費，證交所 OpenAPI）
    回傳 DataFrame，包含各股外資/投信/自營商買賣超張數
    """
    url = "https://openapi.twse.com.tw/v1/fund/T86"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return pd.DataFrame(resp.json())


# ============================================================
# 2. 動態觀察清單：從全市場資料中自動篩選符合條件的股票
# ============================================================

def build_dynamic_watchlist(market_df: pd.DataFrame, inst_df: pd.DataFrame):
    """
    回傳 dict，key 為類別名稱，value 為該類別命中的股票代號清單
    每個類別最多取 DYNAMIC_LIMIT_PER_CATEGORY 檔，避免清單爆量
    """
    result = {}

    # (a) 價格漲跌幅異常
    market_df["漲跌幅%"] = (market_df["漲跌價差"] / (market_df["收盤價"] - market_df["漲跌價差"])) * 100
    price_alert = market_df[market_df["漲跌幅%"].abs() >= PRICE_ALERT_PCT]
    price_alert = price_alert.reindex(
        price_alert["漲跌幅%"].abs().sort_values(ascending=False).index
    ).head(DYNAMIC_LIMIT_PER_CATEGORY)
    result["價格異常"] = price_alert["證券代號"].astype(str).tolist()

    # (b) 成交量前N大（示範用，實務建議改成「當日量 / 20日均量」）
    volume_top = market_df.sort_values("成交股數", ascending=False).head(VOLUME_TOP_N)
    result["成交量異常"] = volume_top["證券代號"].astype(str).tolist()

    # (c) 法人買賣超異動前N大（以外資買超張數排序，欄位名稱依實際回傳調整）
    if not inst_df.empty and "外資買賣超股數" in inst_df.columns:
        inst_df["外資買賣超股數"] = pd.to_numeric(
            inst_df["外資買賣超股數"].astype(str).str.replace(",", ""), errors="coerce"
        )
        inst_top = inst_df.reindex(
            inst_df["外資買賣超股數"].abs().sort_values(ascending=False).index
        ).head(DYNAMIC_LIMIT_PER_CATEGORY)
        result["法人異動"] = inst_top["證券代號"].astype(str).tolist()
    else:
        result["法人異動"] = []

    return result


# ============================================================
# 3. 技術指標運算（示範 MA、KD，實務可換成 pandas_ta 套件算更多指標）
# ============================================================

def compute_simple_signals(market_df: pd.DataFrame, stock_ids: list):
    """
    這裡只示範用「單日資料」判斷收盤價相對開盤價的簡單訊號，
    要算 KD / MACD / MA 交叉，需要「多日歷史資料」，
    建議做法：GitHub Actions 每天把 market_df 存成 CSV 累積歷史，
    之後改成讀取歷史 CSV 再用 pandas_ta 計算。
    """
    subset = market_df[market_df["證券代號"].astype(str).isin(stock_ids)]
    signals = []
    for _, row in subset.iterrows():
        signals.append({
            "代號": row["證券代號"],
            "名稱": row.get("證券名稱", ""),
            "收盤價": row["收盤價"],
            "漲跌幅%": round(row.get("漲跌幅%", 0), 2),
        })
    return signals


# ============================================================
# 4. 推播層：訊息合併成一則，優先用 LINE，備援用 Telegram
# ============================================================

def format_message(core_signals, dynamic_watchlist):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📊 台股每日監控 {today}", ""]

    lines.append("【核心自選股】")
    for s in core_signals:
        lines.append(f"{s['代號']} {s['名稱']}：{s['收盤價']}（{s['漲跌幅%']}%）")

    lines.append("")
    for category, ids in dynamic_watchlist.items():
        if ids:
            lines.append(f"【{category}】命中 {len(ids)} 檔：{', '.join(ids)}")

    return "\n".join(lines)


def send_line_message(text: str) -> bool:
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_TARGET_ID:
        return False
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {"to": LINE_TARGET_ID, "messages": [{"type": "text", "text": text}]}
    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
    return resp.status_code == 200


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    resp = requests.post(url, data=payload, timeout=10)
    return resp.status_code == 200


# ============================================================
# 5. 主流程
# ============================================================

def main():
    market_df = fetch_all_market_daily()

    try:
        inst_df = fetch_institutional_investors()
    except Exception:
        inst_df = pd.DataFrame()

    dynamic_watchlist = build_dynamic_watchlist(market_df, inst_df)
    core_signals = compute_simple_signals(market_df, CORE_WATCHLIST)

    message = format_message(core_signals, dynamic_watchlist)
    print(message)  # 同時印出到 GitHub Actions log，方便除錯

    # 優先送 LINE（額度內），失敗或未設定則走 Telegram 備援
    sent = send_line_message(message)
    if not sent:
        send_telegram_message(message)

    # 存檔累積歷史資料，供之後計算 KD/MACD 等需要多日資料的指標使用
    os.makedirs("data", exist_ok=True)
    today_str = datetime.now().strftime("%Y%m%d")
    market_df.to_csv(f"data/market_{today_str}.csv", index=False, encoding="utf-8-sig")

    # 另外輸出一份「儀表板專用」簡化格式，供 docs/index.html（GitHub Pages）讀取
    write_dashboard_csv(market_df, core_signals, dynamic_watchlist)


def write_dashboard_csv(market_df, core_signals, dynamic_watchlist):
    """
    產生 data/latest.csv，欄位固定為：證券代號,證券名稱,收盤價,漲跌幅%,類別
    這份檔案是給 docs/index.html 的網頁儀表板讀取用，跟歷史備份檔（data/market_*.csv）分開。
    """
    rows = []

    for s in core_signals:
        rows.append({
            "證券代號": s["代號"],
            "證券名稱": s["名稱"],
            "收盤價": s["收盤價"],
            "漲跌幅%": s["漲跌幅%"],
            "類別": "核心自選股",
        })

    lookup = market_df.set_index(market_df["證券代號"].astype(str))
    for category, ids in dynamic_watchlist.items():
        for stock_id in ids:
            if stock_id in CORE_WATCHLIST:
                continue  # 避免跟核心自選股重複列出
            if stock_id not in lookup.index:
                continue
            row = lookup.loc[stock_id]
            rows.append({
                "證券代號": stock_id,
                "證券名稱": row.get("證券名稱", ""),
                "收盤價": row.get("收盤價", ""),
                "漲跌幅%": round(row.get("漲跌幅%", 0), 2),
                "類別": category,
            })

    pd.DataFrame(rows).to_csv("data/latest.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
