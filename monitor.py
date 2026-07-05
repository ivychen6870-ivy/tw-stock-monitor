"""
台股每日監控機器人 - 免費架構版本
資料源：證交所 OpenAPI（免費）
運算：pandas（開源）
推播：LINE Messaging API（每月200則內）+ Telegram Bot（免費備援）

執行方式：由 GitHub Actions 每日收盤後排程觸發

重要：所有輸出資料都存在 docs/data/ 底下（不是 data/），
因為 GitHub Pages 只會公開發布 docs/ 資料夾，資料要放進去網頁才讀得到。
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

# 成交量異常門檻（範例：當日量前N大，實務上可改成「當日量 / 20日均量」）
VOLUME_TOP_N = 10

# 資料輸出目錄：一定要在 docs/ 底下，GitHub Pages 才讀得到
DATA_DIR = "docs/data"

# 歷史資料保留天數（以「有執行程式的交易日」為單位，非日曆天）
# 半年約 21 個交易日 * 6 個月 ≈ 126 天，抓寬一點設 130
HISTORY_MAX_DAYS = 130

# LINE / Telegram 憑證，從 GitHub Actions 的 Secrets 帶入，不要寫死在程式裡
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TARGET_ID = os.environ.get("LINE_TARGET_ID", "")  # 使用者或群組 ID
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# GitHub Actions 會自動提供這兩個環境變數，不需要另外申請/設定
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # 格式："帳號/repo名稱"


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
    numeric_cols = ["成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價", "漲跌價差", "成交筆數"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", ""), errors="coerce")
    return df


def fetch_institutional_investors():
    """抓取上市三大法人買賣超（免費，證交所 OpenAPI）"""
    url = "https://openapi.twse.com.tw/v1/fund/T86"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return pd.DataFrame(resp.json())


def fetch_material_announcements(limit: int = 15):
    """
    抓取上市公司「重大訊息」公告（免費，證交所 OpenAPI，端點 t187ap04_L）
    這份資料就是公開資訊觀測站(MOPS)的重大訊息來源，用來當作網頁「今日新聞」區塊的資料。
    """
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    df = pd.DataFrame(resp.json())
    if df.empty:
        return df

    code_col = next((c for c in df.columns if "公司代號" in c or "證券代號" in c), None)
    name_col = next((c for c in df.columns if "公司名稱" in c or "證券名稱" in c), None)
    subject_col = next((c for c in df.columns if "主旨" in c or "訊息內容" in c), None)
    date_col = next((c for c in df.columns if "發言日期" in c or "出表日期" in c), None)
    time_col = next((c for c in df.columns if "發言時間" in c), None)

    out = pd.DataFrame({
        "代號": df[code_col] if code_col else "",
        "名稱": df[name_col] if name_col else "",
        "主旨": df[subject_col] if subject_col else "",
        "日期": df[date_col] if date_col else "",
        "時間": df[time_col] if time_col else "",
    })
    return out.head(limit)


# ============================================================
# 2. 關注股票請求：讀取網頁產生的 GitHub Issue，合併進監控清單
# ============================================================

def fetch_watch_requests():
    """
    讀取 repo 裡標題開頭是「watch-request:」的開放 Issue（網頁勾選後會產生這種 Issue），
    解析出裡面的股票代號，回傳 (股票代號清單, 對應的 issue 編號清單)。
    這是公開讀取的 GitHub API，不需要金鑰。
    """
    if not GITHUB_REPOSITORY:
        return [], []

    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues"
    try:
        resp = requests.get(url, params={"state": "open", "per_page": 50}, timeout=10)
        resp.raise_for_status()
        issues = resp.json()
    except Exception as e:
        print(f"讀取 watch-request issues 失敗：{e}")
        return [], []

    codes = []
    issue_numbers = []
    for issue in issues:
        title = issue.get("title", "")
        if title.startswith("watch-request:"):
            raw = title.replace("watch-request:", "").strip()
            for code in raw.split(","):
                code = code.strip()
                if code:
                    codes.append(code)
            issue_numbers.append(issue["number"])

    return codes, issue_numbers


def close_issue(issue_number: int):
    """處理完 watch-request 後，把該 Issue 關閉，避免下次重複處理"""
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues/{issue_number}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        requests.patch(url, headers=headers, json={"state": "closed"}, timeout=10)
    except Exception as e:
        print(f"關閉 issue #{issue_number} 失敗：{e}")


def load_extra_watchlist() -> list:
    """讀取之前已經核准過的額外關注股票清單（存在 docs/data/watchlist.json）"""
    path = os.path.join(DATA_DIR, "watchlist.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f).get("extra", [])
        except Exception:
            return []
    return []


def save_extra_watchlist(codes: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "watchlist.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"core": CORE_WATCHLIST, "extra": codes}, f, ensure_ascii=False)


# ============================================================
# 3. 動態觀察清單：從全市場資料中自動篩選符合條件的股票
# ============================================================

def build_dynamic_watchlist(market_df: pd.DataFrame, inst_df: pd.DataFrame):
    result = {}

    market_df["漲跌幅%"] = (market_df["漲跌價差"] / (market_df["收盤價"] - market_df["漲跌價差"])) * 100
    price_alert = market_df[market_df["漲跌幅%"].abs() >= PRICE_ALERT_PCT]
    price_alert = price_alert.reindex(
        price_alert["漲跌幅%"].abs().sort_values(ascending=False).index
    ).head(DYNAMIC_LIMIT_PER_CATEGORY)
    result["價格異常"] = price_alert["證券代號"].astype(str).tolist()

    volume_top = market_df.sort_values("成交股數", ascending=False).head(VOLUME_TOP_N)
    result["成交量異常"] = volume_top["證券代號"].astype(str).tolist()

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
# 4. 歷史 OHLC 紀錄 + 技術指標（MA / KD），供 K 線圖使用
# ============================================================

HISTORY_FILE_NAME = "history.json"


def compute_ma(closes: list, period: int):
    """簡單移動平均，資料不夠的天數回傳 None"""
    result = []
    for i in range(len(closes)):
        if i + 1 < period:
            result.append(None)
        else:
            window = closes[i + 1 - period:i + 1]
            result.append(round(sum(window) / period, 2))
    return result


def compute_kd(highs: list, lows: list, closes: list, period: int = 9):
    """
    經典 KD 指標（隨機指標）計算：
    RSV = (收盤 - N日內最低) / (N日內最高 - N日內最低) * 100
    K = 前一日K * 2/3 + RSV * 1/3（起始值設50）
    D = 前一日D * 2/3 + K * 1/3（起始值設50）
    """
    k_list, d_list = [], []
    prev_k, prev_d = 50.0, 50.0
    for i in range(len(closes)):
        if i + 1 < period:
            k_list.append(None)
            d_list.append(None)
            continue
        window_high = max(highs[i + 1 - period:i + 1])
        window_low = min(lows[i + 1 - period:i + 1])
        rsv = 50.0 if window_high == window_low else (closes[i] - window_low) / (window_high - window_low) * 100
        k = prev_k * 2 / 3 + rsv * 1 / 3
        d = prev_d * 2 / 3 + k * 1 / 3
        k_list.append(round(k, 2))
        d_list.append(round(d, 2))
        prev_k, prev_d = k, d
    return k_list, d_list


def update_price_history(market_df: pd.DataFrame, watch_ids: list) -> dict:
    """
    讀取既有的 docs/data/history.json，把今天監控股票的 OHLC 加進去，
    重新計算 MA5 / MA20 / KD，存回檔案，供網頁畫 K 線圖使用。
    watch_ids：核心自選股 + 已核准的關注股票，這些才會有完整OHLC歷史。
    """
    path = os.path.join(DATA_DIR, HISTORY_FILE_NAME)
    history = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = {}

    today = datetime.now().strftime("%Y-%m-%d")
    lookup = market_df.set_index(market_df["證券代號"].astype(str))

    for stock_id in watch_ids:
        if stock_id not in lookup.index:
            continue
        row = lookup.loc[stock_id]
        entry = {
            "date": today,
            "open": float(row.get("開盤價", 0) or 0),
            "high": float(row.get("最高價", 0) or 0),
            "low": float(row.get("最低價", 0) or 0),
            "close": float(row.get("收盤價", 0) or 0),
        }
        series = history.get(stock_id, [])
        series = [h for h in series if h["date"] != today]  # 避免同日重複執行造成重複
        series.append(entry)
        series = series[-HISTORY_MAX_DAYS:]

        closes = [h["close"] for h in series]
        highs = [h["high"] for h in series]
        lows = [h["low"] for h in series]
        ma5 = compute_ma(closes, 5)
        ma20 = compute_ma(closes, 20)
        k_vals, d_vals = compute_kd(highs, lows, closes)
        for i, h in enumerate(series):
            h["ma5"] = ma5[i]
            h["ma20"] = ma20[i]
            h["k"] = k_vals[i]
            h["d"] = d_vals[i]

        history[stock_id] = series

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)

    return history


def compute_simple_signals(market_df: pd.DataFrame, stock_ids: list):
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
# 5. 推播層：訊息合併成一則，優先用 LINE，備援用 Telegram
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
# 6. 主流程
# ============================================================

def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    # 6.1 處理網頁送出的「關注股票」請求（GitHub Issue），合併進監控清單
    requested_codes, issue_numbers = fetch_watch_requests()
    extra_watchlist = load_extra_watchlist()
    for code in requested_codes:
        if code not in extra_watchlist:
            extra_watchlist.append(code)
    save_extra_watchlist(extra_watchlist)
    for n in issue_numbers:
        close_issue(n)

    watch_ids = list(dict.fromkeys(CORE_WATCHLIST + extra_watchlist))  # 去重，保留順序

    # 6.2 抓取市場資料
    market_df = fetch_all_market_daily()
    try:
        inst_df = fetch_institutional_investors()
    except Exception:
        inst_df = pd.DataFrame()

    dynamic_watchlist = build_dynamic_watchlist(market_df, inst_df)
    core_signals = compute_simple_signals(market_df, watch_ids)

    # 6.3 推播（核心自選股 + 使用者額外關注的股票，都會被推播提醒）
    message = format_message(core_signals, dynamic_watchlist)
    print(message)
    sent = send_line_message(message)
    if not sent:
        send_telegram_message(message)

    # 6.4 存檔：全市場備份（放 docs/data 底下，供之後擴充查詢用）
    today_str = datetime.now().strftime("%Y%m%d")
    market_df.to_csv(os.path.join(DATA_DIR, f"market_{today_str}.csv"), index=False, encoding="utf-8-sig")

    # 6.5 儀表板專用簡化格式
    write_dashboard_csv(market_df, core_signals, dynamic_watchlist)

    # 6.6 更新 OHLC 歷史 + 技術指標（MA5/MA20/KD），供 K 線圖使用
    update_price_history(market_df, watch_ids)

    # 6.7 今日新聞（重大訊息公告）
    try:
        news_df = fetch_material_announcements()
        news_df.to_csv(os.path.join(DATA_DIR, "news.csv"), index=False, encoding="utf-8-sig")
    except Exception as e:
        print(f"抓取重大訊息公告失敗，略過本次新聞更新：{e}")


def write_dashboard_csv(market_df, core_signals, dynamic_watchlist):
    """
    產生 docs/data/latest.csv，欄位固定為：證券代號,證券名稱,收盤價,漲跌幅%,類別
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
    existing_codes = {str(s["代號"]) for s in core_signals}
    for category, ids in dynamic_watchlist.items():
        for stock_id in ids:
            if stock_id in existing_codes:
                continue
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

    pd.DataFrame(rows).to_csv(os.path.join(DATA_DIR, "latest.csv"), index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
