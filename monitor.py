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
from collections import defaultdict

# ============================================================
# 0. 設定區 —— 之後全部改成你自己的清單/門檻即可
# ============================================================

# 固定層：你手動關注的核心自選股（股票代號）
# 注意：5309(系統電)是上櫃股票(TPEx)，目前資料源只抓上市(TWSE)，暫時不會有資料
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

# 推播訊息裡，每個動態分類最多顯示幾檔（避免訊息太長），網頁則會顯示完整的 DYNAMIC_LIMIT_PER_CATEGORY 檔數
PUSH_MESSAGE_TOP_N = 3

# ============================================================
# 投資論點維護（/thesis 簡化版）—— 規則式比對，不是AI生成文字
# ============================================================
# 在這裡記錄你對特定股票的投資假設，系統會拿最新月營收年增率跟你設定的門檻比對，
# 幫你自動盯著「數字有沒有偏離原本的假設」，不用自己每個月手動去查。
#
# 格式：股票代號: {"revenue_yoy_min": 門檻百分比, "note": 你的論點簡述}
# revenue_yoy_min 代表「你預期這檔股票的月營收年增率至少要達到多少%」，
# 實際年增率低於這個門檻，系統就會標記「低於預期，建議重新檢視論點」。
INVESTMENT_THESIS = {
    "2330": {"revenue_yoy_min": 20, "note": "看好AI晶片需求持續帶動先進製程營收成長"},
    "3481": {"revenue_yoy_min": 0, "note": "面板產業止跌回升，觀察營收是否轉正"},
    "3324": {"revenue_yoy_min": 10, "note": "AI伺服器散熱需求，預期營收維持雙位數成長"},
}

# ============================================================
# 催化事件追蹤（/catalysts 簡化版）—— 法說會／股東會日期
# 全部使用證交所免費 OpenAPI，不需要金鑰，不會產生額外費用
# ============================================================
# 追蹤股票清單，預設沿用核心自選股，也可以改成只列你關心的代號
CATALYST_WATCHLIST = CORE_WATCHLIST

# LINE推播只在事件倒數幾天內才提醒（避免太早知道就一直被打擾），網頁則會顯示所有未來事件
CATALYST_ALERT_DAYS_AHEAD = 7

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

# 測試用開關：手動觸發workflow時如果設成 "true"，會跳過真正的LINE/Telegram推播，
# 只在log裡印出訊息內容，避免測試時浪費LINE每月200則的免費額度
SKIP_PUSH = os.environ.get("SKIP_PUSH", "false").lower() == "true"
LINE_TARGET_ID = os.environ.get("LINE_TARGET_ID", "")  # 使用者或群組 ID
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# GitHub Actions 會自動提供這兩個環境變數，不需要另外申請/設定
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # 格式："帳號/repo名稱"


# ============================================================
# 1. 資料源層：抓取「全市場」當日資料（一次打包，不用逐檔查）
# ============================================================

def fetch_otc_market_daily():
    """
    抓取上櫃全部個股當日成交資訊（櫃買中心 TPEx OpenAPI，免費，無需金鑰）。
    回傳 DataFrame，欄位跟 fetch_all_market_daily() 對齊：證券代號、證券名稱、開盤價、
    最高價、最低價、收盤價、漲跌價差、成交股數，這樣兩份資料可以直接合併使用。

    ⚠️ 櫃買中心這個端點的資料結構跟證交所很不一樣：不是單純一份列表，
    而是把好幾種統計資料包在同一份回應裡（data1~data9 這種區塊），
    個股的收盤行情藏在其中一個區塊（通常是最後一個），且必須帶「民國年日期」參數，
    不像證交所那樣不帶日期就自動給今天最新資料。
    這裡用「欄位名稱關鍵字比對」而不是寫死欄位順序，比較能適應櫃買中心調整過格式的情況；
    如果真的抓不到，會印出實際收到的欄位讓人可以直接對照修正，不會讓程式掛掉。
    """
    empty = pd.DataFrame(columns=["證券代號", "證券名稱", "開盤價", "最高價", "最低價", "收盤價", "漲跌價差", "成交股數"])

    now = datetime.now()
    roc_date = f"{now.year - 1911}/{now.month:02d}/{now.day:02d}"
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    params = {"l": "zh-tw", "d": roc_date}

    try:
        resp = requests.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"抓取上櫃股票資料失敗，略過本次上櫃更新：{e}")
        return empty

    # 找出「個股收盤行情」那個資料區塊：掃過 data1~data9，找欄位名稱裡同時有「代號」跟「收盤」的那一塊
    rows, fields = None, None
    for i in range(1, 10):
        block_fields = data.get(f"fields{i}")
        block_data = data.get(f"data{i}")
        if not block_fields or not block_data:
            continue
        field_text = "".join(block_fields)
        if "代號" in field_text and "收盤" in field_text:
            rows, fields = block_data, block_fields
            break

    if rows is None:
        print(f"上櫃股票資料抓不到對應的收盤行情區塊，回應裡的區塊：{[k for k in data.keys() if k.startswith('fields')]}，略過本次上櫃更新")
        return empty

    try:
        df = pd.DataFrame(rows, columns=fields)
    except Exception as e:
        print(f"上櫃股票資料欄位對不上（{e}），略過本次上櫃更新")
        return empty

    def find_col(keyword):
        return next((c for c in df.columns if keyword in c), None)

    col_map = {
        "證券代號": find_col("代號"), "證券名稱": find_col("名稱"),
        "開盤價": find_col("開盤"), "最高價": find_col("最高"), "最低價": find_col("最低"),
        "收盤價": find_col("收盤"), "漲跌價差": find_col("漲跌"), "成交股數": find_col("成交股數"),
    }
    missing = [k for k, v in col_map.items() if v is None]
    if missing:
        print(f"上櫃股票資料缺少欄位：{missing}，實際欄位：{list(df.columns)}，略過本次上櫃更新")
        return empty

    out = pd.DataFrame({new: df[old] for new, old in col_map.items()})
    out["證券代號"] = out["證券代號"].astype(str).str.strip()
    numeric_cols = ["開盤價", "最高價", "最低價", "收盤價", "漲跌價差", "成交股數"]
    for col in numeric_cols:
        out[col] = pd.to_numeric(out[col].astype(str).str.replace(",", ""), errors="coerce")
    return out


def fetch_all_market_daily():
    """
    抓取上市全部個股當日成交資訊（政府開放資料，免費，無需金鑰）
    回傳 DataFrame，欄位包含：證券代號、證券名稱、開盤價、最高價、最低價、
    收盤價、漲跌價差、成交股數 等
    """
    url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()

    # dtype強制指定證券代號為文字，避免像 0050 這種開頭有0的代號被誤判成數字50，
    # 導致跟股票池比對代號時對不起來、或顯示時遺失開頭的0
    df = pd.read_csv(pd.io.common.StringIO(resp.text), dtype={"證券代號": str})
    df["證券代號"] = df["證券代號"].str.strip()
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


def fetch_monthly_revenue(stock_ids: list) -> dict:
    """
    抓取上市公司月營收（免費，證交所 OpenAPI，端點 t187ap05_L），
    回傳 { 股票代號: 年增率% }，只保留 stock_ids 裡有的股票。

    這個端點沒有正式的欄位文件，用「欄位名稱關鍵字比對」盡量抓對，
    如果格式跟預期不同，對應股票的年增率會回傳 None，不會讓程式掛掉。
    """
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        df = pd.DataFrame(resp.json())
    except Exception as e:
        print(f"抓取月營收失敗：{e}")
        return {}

    if df.empty:
        return {}

    def find_col(*keywords):
        for c in df.columns:
            if all(k in c for k in keywords):
                return c
        return None

    code_col = find_col("公司代號")
    yoy_col = find_col("去年同月增減")  # 證交所通常會直接算好年增率百分比
    cur_rev_col = find_col("當月營收") or find_col("營業收入", "當月")
    last_year_rev_col = find_col("去年當月營收") or find_col("去年", "當月", "營收")

    result = {}
    for _, row in df.iterrows():
        if code_col is None:
            break
        code = str(row[code_col]).strip()
        if code not in stock_ids:
            continue
        yoy = None
        try:
            if yoy_col is not None:
                raw = str(row[yoy_col]).replace(",", "").replace("%", "").strip()
                if raw not in ("", "-", "nan"):
                    yoy = round(float(raw), 2)
            elif cur_rev_col is not None and last_year_rev_col is not None:
                cur = float(str(row[cur_rev_col]).replace(",", ""))
                last = float(str(row[last_year_rev_col]).replace(",", ""))
                if last:
                    yoy = round((cur - last) / last * 100, 2)
        except (ValueError, TypeError):
            yoy = None
        result[code] = yoy

    return result


def check_investment_thesis(monthly_revenue: dict, name_lookup: dict = None) -> list:
    """
    比對 INVESTMENT_THESIS 裡設定的假設 vs 實際月營收年增率，
    回傳每檔股票的比對結果，供推播訊息跟網頁顯示。
    這是規則式比對（數字 vs 門檻），不是AI生成的分析文字。
    name_lookup 是 {股票代號: 股票名稱} 的對照表，找不到時名稱留空字串，不會讓程式掛掉。
    """
    name_lookup = name_lookup or {}
    results = []
    for code, thesis in INVESTMENT_THESIS.items():
        yoy = monthly_revenue.get(code)
        threshold = thesis.get("revenue_yoy_min", 0)
        note = thesis.get("note", "")

        if yoy is None:
            status = "本月營收資料尚未公布或抓取失敗"
        elif yoy >= threshold:
            status = f"符合預期（實際年增{yoy}% ≥ 門檻{threshold}%）"
        else:
            status = f"低於預期（實際年增{yoy}% < 門檻{threshold}%），建議重新檢視論點"

        results.append({
            "代號": code, "名稱": name_lookup.get(code, ""),
            "門檻%": threshold, "實際年增%": yoy, "狀態": status, "備註": note,
        })
    return results


def _roc_date_to_display(raw: str):
    """民國日期字串（如"1150620"）轉成好讀格式"115/06/20"，格式不對就照原樣回傳"""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) >= 7:
        return f"{digits[:3]}/{digits[3:5]}/{digits[5:7]}"
    return raw


def fetch_shareholder_meeting_dates(stock_ids: list) -> dict:
    """
    股東會日期 —— 證交所「股利分派情形」OpenAPI（t187ap45_L），免費、不需金鑰。
    這份資料原本是揭露股利分派用的，但股東會日期是同一次公告出來的，
    是目前唯一「結構化欄位、非HTML爬蟲」能拿到股東會日期的免費資料源。
    回傳 { 股票代號: 股東會日期（民國年月日字串，如"1150620"）}
    """
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap45_L"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        df = pd.DataFrame(resp.json())
    except Exception as e:
        print(f"抓取股東會日期失敗：{e}")
        return {}
    if df.empty:
        return {}

    code_col = next((c for c in df.columns if "公司代號" in c), None)
    date_col = next((c for c in df.columns if "股東會日期" in c), None)
    if code_col is None or date_col is None:
        return {}

    result = {}
    for _, row in df.iterrows():
        code = str(row[code_col]).strip()
        if code not in stock_ids:
            continue
        raw = str(row[date_col]).strip()
        if raw and raw not in ("", "nan", "-"):
            result[code] = raw  # 同代號多筆時，後面覆蓋前面，保留最後一筆
    return result


def extract_date_from_subject(subject: str):
    """
    從公告主旨文字裡，用常見民國日期格式（如「115年07月15日」「115/07/15」）盡量解析出日期，
    抓不到就回傳 None——這是文字解析，不保證100%準確，不會讓程式掛掉。
    """
    import re
    m = re.search(r"(\d{2,3})[年/-](\d{1,2})[月/-](\d{1,2})日?", subject)
    if m:
        return f"{m.group(1)}/{m.group(2).zfill(2)}/{m.group(3).zfill(2)}"
    return None


def fetch_investor_conference_events(stock_ids: list, news_df) -> list:
    """
    法說會（法人說明會）事件 —— 證交所OpenAPI沒有「未來排定法說會」的結構化端點，
    只有「本年度累計已召開次數」，所以改比對已經在抓的「重大訊息」公告（t187ap04_L，免費），
    看主旨裡有沒有「法說會」「法人說明會」字樣。

    重要限制（老實說）：只有公司「有」透過重大訊息公告法說會時程時才抓得到，
    不是100%涵蓋所有公司；日期是從主旨文字解析，抓不到時「日期」為 None，
    網頁會顯示「詳見備註」而不是讓程式掛掉。
    """
    if news_df is None or news_df.empty:
        return []
    events = []
    for _, row in news_df.iterrows():
        code = str(row.get("代號", "")).strip()
        if code not in stock_ids:
            continue
        subject = str(row.get("主旨", ""))
        if ("法說會" in subject) or ("法人說明會" in subject):
            events.append({
                "代號": code,
                "名稱": row.get("名稱", ""),
                "類型": "法說會",
                "日期": extract_date_from_subject(subject),
                "備註": subject[:60],
            })
    return events


def build_catalyst_events(stock_ids: list, news_df, name_lookup: dict = None) -> list:
    """
    組合股東會日期 + 法說會事件，並濾掉「日期解析得出來、但已經過期」的事件，
    避免網頁跟推播出現一堆已經開完的舊股東會。
    日期解析不出來的事件（法說會常見）會保留，因為無法判斷是否過期，讓你自己點進備註確認。
    name_lookup 是 {股票代號: 股票名稱} 的對照表，找不到時名稱留空字串。
    """
    name_lookup = name_lookup or {}
    today = datetime.now()
    events = []

    meeting_dates = fetch_shareholder_meeting_dates(stock_ids)
    for code, raw_date in meeting_dates.items():
        events.append({
            "代號": code, "名稱": name_lookup.get(code, ""), "類型": "股東會",
            "日期": _roc_date_to_display(raw_date), "備註": "資料來源：股利分派情形公告",
        })

    events += fetch_investor_conference_events(stock_ids, news_df)

    kept = []
    for e in events:
        if not e["日期"]:
            kept.append(e)  # 解析不出日期，無法判斷過期與否，保留讓你自己看備註
            continue
        try:
            y, m, d = e["日期"].split("/")
            event_date = datetime(int(y) + 1911, int(m), int(d))
            if event_date.date() >= today.date():
                kept.append(e)
        except Exception:
            kept.append(e)  # 解析失敗也保留，不要因為格式問題把資料弄丟

    kept.sort(key=lambda e: e["日期"] or "9999/99/99")
    return kept


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

def is_etf_code(code: str) -> bool:
    """
    台股 ETF（含美債ETF、槓桿/反向ETF）代號幾乎都是「00」開頭，例如：
    0050（元大台灣50）、00878（國泰永續高股息）、00679B（美債20年ETF）、00632R（元大台灣50反1）
    用這個規則排除，不會影響你自己手動設定的核心自選股（那是另外獨立的清單）。
    """
    code = str(code).strip()
    return code.startswith("00")


def build_dynamic_watchlist(market_df: pd.DataFrame, inst_df: pd.DataFrame):
    result = {}

    market_df["漲跌幅%"] = (market_df["漲跌價差"] / (market_df["收盤價"] - market_df["漲跌價差"])) * 100

    # 動態篩選只在「非ETF」的股票池裡找，避免每天的潛力股/異動清單被ETF洗版
    stock_pool = market_df[~market_df["證券代號"].astype(str).apply(is_etf_code)].copy()

    price_alert = stock_pool[stock_pool["漲跌幅%"].abs() >= PRICE_ALERT_PCT]
    price_alert = price_alert.reindex(
        price_alert["漲跌幅%"].abs().sort_values(ascending=False).index
    ).head(DYNAMIC_LIMIT_PER_CATEGORY)
    result["價格異常"] = price_alert["證券代號"].astype(str).tolist()

    volume_top = stock_pool.sort_values("成交股數", ascending=False).head(VOLUME_TOP_N)
    result["成交量異常"] = volume_top["證券代號"].astype(str).tolist()

    if not inst_df.empty and "外資買賣超股數" in inst_df.columns:
        inst_pool = inst_df[~inst_df["證券代號"].astype(str).apply(is_etf_code)].copy()
        inst_pool["外資買賣超股數"] = pd.to_numeric(
            inst_pool["外資買賣超股數"].astype(str).str.replace(",", ""), errors="coerce"
        )
        inst_top = inst_pool.reindex(
            inst_pool["外資買賣超股數"].abs().sort_values(ascending=False).index
        ).head(DYNAMIC_LIMIT_PER_CATEGORY)
        result["法人異動"] = inst_top["證券代號"].astype(str).tolist()
    else:
        result["法人異動"] = []

    # (d) 市場熱度潛力股：綜合「成交金額排名」+「漲幅排名」，抓當日市場關注度最高、動能最強的股票
    # 只看上漲的股票（漲幅為正），避免把重挫但成交量大的股票也算進「潛力股」；已排除ETF
    heat_df = stock_pool[stock_pool["漲跌幅%"] > 0].copy()
    if not heat_df.empty and "成交金額" in heat_df.columns:
        heat_df["金額排名"] = heat_df["成交金額"].rank(ascending=False)
        heat_df["漲幅排名"] = heat_df["漲跌幅%"].rank(ascending=False)
        heat_df["熱度分數"] = heat_df["金額排名"] + heat_df["漲幅排名"]  # 分數越小代表越熱門
        heat_top = heat_df.sort_values("熱度分數").head(DYNAMIC_LIMIT_PER_CATEGORY)
        result["熱度潛力股"] = heat_top["證券代號"].astype(str).tolist()
    else:
        result["熱度潛力股"] = []

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


def compute_ema(closes: list, period: int):
    """指數移動平均線（EMA），資料不夠時回傳 None。是MACD計算的基礎。"""
    n = len(closes)
    result = [None] * n
    if n < period:
        return result
    multiplier = 2 / (period + 1)
    result[period - 1] = sum(closes[:period]) / period  # 第一個值用簡單移動平均起算
    for i in range(period, n):
        result[i] = (closes[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def compute_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    MACD 指標：DIF（快線-慢線）與 DEA（信號線，DIF的9日EMA）。
    DIF 由下往上穿越 DEA 是常見的「MACD黃金交叉」買進參考訊號，反之為死亡交叉。
    回傳 (dif_list, dea_list)，資料不夠的天數為 None。
    """
    n = len(closes)
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    dif = [None] * n
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif[i] = ema_fast[i] - ema_slow[i]

    valid_idx = [i for i, v in enumerate(dif) if v is not None]
    dea = [None] * n
    if len(valid_idx) >= signal:
        dif_valid = [dif[i] for i in valid_idx]
        ema_of_dif = compute_ema(dif_valid, signal)
        for idx, val in zip(valid_idx, ema_of_dif):
            dea[idx] = val

    dif = [round(v, 3) if v is not None else None for v in dif]
    dea = [round(v, 3) if v is not None else None for v in dea]
    return dif, dea


def compute_rsi(closes: list, period: int = 14):
    """
    RSI（相對強弱指標），用經典的 Wilder's Smoothing 算法。
    RSI ≥ 70 一般視為「超買」，RSI ≤ 30 視為「超賣」。
    回傳 rsi_list，資料不夠的天數為 None。
    """
    n = len(closes)
    result = [None] * n
    if n < period + 1:
        return result

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = max(change, 0)
        losses[i] = max(-change, 0)

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    result[period] = 100.0 if avg_loss == 0 else round(100 - 100 / (1 + avg_gain / avg_loss), 2)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i] = 100.0 if avg_loss == 0 else round(100 - 100 / (1 + avg_gain / avg_loss), 2)

    return result


def compute_bollinger_bands(closes: list, period: int = 20, num_std: float = 2.0):
    """
    布林通道：中軌是MA20（跟你現有的MA20是同一條線），上下軌是中軌 ± N倍標準差，
    用來衡量「股價相對於自己近期的正常波動範圍，現在是不是偏離太多」，
    這跟MA交叉、KD、MACD這些「兩條線交叉」的邏輯不一樣，是額外的一個視角。
    也可以用「通道寬窄」判斷目前是盤整（通道窄）還是趨勢明顯（通道寬）。
    回傳 (upper_list, middle_list, lower_list)，資料不夠的天數為 None。
    """
    n = len(closes)
    upper, middle, lower = [None] * n, [None] * n, [None] * n
    if n < period:
        return upper, middle, lower

    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5
        middle[i] = round(mean, 3)
        upper[i] = round(mean + num_std * std, 3)
        lower[i] = round(mean - num_std * std, 3)

    return upper, middle, lower


def compute_atr(highs: list, lows: list, closes: list, period: int = 14):
    """
    ATR（真實波動幅度均值），用 Wilder's Smoothing（跟RSI同一種算法）。
    衡量這檔股票「正常情況下」一天大概會震盪多少，用來判斷「今天的漲跌算不算異常」，
    而不是用同一個固定百分比套用在所有股票上——波動大的股票（例如航運股）
    跟波動小的股票（例如金融股），「異常」的定義本來就不該一樣。
    回傳 atr_list，資料不夠的天數為 None。
    """
    n = len(closes)
    result = [None] * n
    if n < period + 1:
        return result

    tr_list = [None] * n
    for i in range(1, n):
        tr_list[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    first_window = [v for v in tr_list[1:period + 1] if v is not None]
    if len(first_window) < period:
        return result

    atr = sum(first_window) / period
    result[period] = round(atr, 3)
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + tr_list[i]) / period
        result[i] = round(atr, 3)

    return result


def fetch_dividend_events(stock_id: str, start_date: str, end_date: str) -> list:
    """
    抓取指定股票在區間內的除權息事件（免費，證交所TWT49U端點）。
    回傳依日期排序：[{"date":"2026-06-15","cash_dividend":2.5,"stock_dividend_rate":0.0}, ...]

    ⚠️ 這個端點沒有正式的欄位文件，是用「欄位名稱關鍵字比對」的方式盡量抓對，
    如果證交所改版格式，這裡會安靜地回傳空清單，不會讓整個系統掛掉，
    但代表「還原股價」這個功能當下沒有生效，股價會退回原始未調整版本。
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
    還原股價（回溯調整法）：對每一次除權息事件，往回把「除權息日之前」的所有OHLC
    乘上一個調整係數，讓價格連續，不會因為配股配息而出現假的跳空缺口。
    公式參考證交所公告：除權息參考價 = (除權息前收盤價 - 現金股利) / (1 + 無償配股率)
    最新的價格維持不變（不是真的可以交易的「還原價」，純粹是用來讓技術指標判斷更準確）。
    """
    if not events:
        return series
    series = [dict(s) for s in series]  # 複製一份，不動到原始資料
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

TAIEX_ID = "TAIEX"  # 大盤指數在history.json裡的特殊代號鍵值，不是真的股票代號


def fetch_taiex_today() -> dict:
    """
    抓取當日大盤（發行量加權股價指數）的開高低收，免費、不需金鑰。
    資料源：證交所網頁版指數資料（MI_5MINS_HIST）。

    ⚠️ 這個端點沒有像 STOCK_DAY_ALL 那樣經過長期驗證，如果證交所調整過欄位順序或路徑，
    這裡會抓不到資料並印出錯誤訊息，但不會讓整個程式掛掉——只是大盤指數這次不會更新。
    回傳 {"open":..., "high":..., "low":..., "close":...} 或 None（抓取失敗時）
    """
    today_str = datetime.now().strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/indicesReport/MI_5MINS_HIST?response=json&date={today_str}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data", [])
        if not rows:
            return None
        last = rows[-1]  # 當天最後一筆（收盤）
        return {
            "open": float(str(last[1]).replace(",", "")),
            "high": float(str(last[2]).replace(",", "")),
            "low": float(str(last[3]).replace(",", "")),
            "close": float(str(last[4]).replace(",", "")),
        }
    except Exception as e:
        print(f"抓取大盤指數失敗，略過本次更新：{e}")
        return None


def _attach_technical_indicators(series: list) -> list:
    """
    幫一組OHLC歷史資料（個股或大盤指數都適用）計算MA5/MA20/MA60/KD/MACD/RSI，
    並直接寫回每一天的資料裡。個股歷史更新跟大盤指數更新共用這個函式，避免同樣的計算邏輯寫兩次。
    """
    closes = [h["close"] for h in series]
    highs = [h["high"] for h in series]
    lows = [h["low"] for h in series]
    ma5 = compute_ma(closes, 5)
    ma20 = compute_ma(closes, 20)
    ma60 = compute_ma(closes, 60)
    k_vals, d_vals = compute_kd(highs, lows, closes)
    dif_vals, dea_vals = compute_macd(closes)
    rsi_vals = compute_rsi(closes)
    atr_vals = compute_atr(highs, lows, closes)
    bb_upper, bb_middle, bb_lower = compute_bollinger_bands(closes)
    for i, h in enumerate(series):
        h["ma5"] = ma5[i]
        h["ma20"] = ma20[i]
        h["ma60"] = ma60[i]
        h["k"] = k_vals[i]
        h["d"] = d_vals[i]
        h["dif"] = dif_vals[i]
        h["dea"] = dea_vals[i]
        h["rsi"] = rsi_vals[i]
        h["atr"] = atr_vals[i]
        h["bb_upper"] = bb_upper[i]
        h["bb_middle"] = bb_middle[i]
        h["bb_lower"] = bb_lower[i]
    return series


def fetch_index_month_ohlc(year: int, month: int) -> list:
    """
    抓取大盤指數（發行量加權股價指數）某個月份的完整每日開高低收，用途是一次性回補歷史資料。
    資料源：證交所網頁版指數資料（MI_5MINS_HIST），帶該月1號當日期參數，會回傳整個月的資料
    （用法跟個股的 STOCK_DAY 端點一樣，帶月初日期就會回傳整月）。
    回傳 [{"date":"2026-01-05","open":...,"high":...,"low":...,"close":...}, ...]
    """
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
            roc_date = row[0]  # 民國年日期，格式如 "115/01/05"
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


def update_price_history(market_df: pd.DataFrame, watch_ids: list) -> dict:
    """
    讀取既有的 docs/data/history.json，把今天監控股票的 OHLC 加進去，
    重新計算 MA5 / MA20 / KD，存回檔案，供網頁畫 K 線圖使用。
    watch_ids：核心自選股 + 已核准的關注股票，這些才會有完整OHLC歷史。

    每次執行也會檢查「今天」是不是這檔股票的除權息日，如果是，
    會自動把歷史資料往回做「還原股價」調整，避免除權息造成的假交叉訊號。
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
    today_str_compact = datetime.now().strftime("%Y%m%d")
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
            "volume": float(row.get("成交股數", 0) or 0),
        }
        series = history.get(stock_id, [])
        series = [h for h in series if h["date"] != today]  # 避免同日重複執行造成重複
        series.append(entry)
        series = series[-HISTORY_MAX_DAYS:]

        # 檢查今天是否為除權息日，若是則回溯調整過去的價格（還原股價）
        try:
            events_today = fetch_dividend_events(stock_id, today_str_compact, today_str_compact)
            if events_today:
                series = adjust_series_for_dividends(series, events_today)
        except Exception as e:
            print(f"{stock_id} 除權息調整檢查失敗，略過本次調整：{e}")

        series = _attach_technical_indicators(series)
        history[stock_id] = series

    # 大盤指數（發行量加權股價指數）：跟個股用同一套技術指標算法，但不是股票池的一部分，獨立處理
    taiex_today = fetch_taiex_today()
    if taiex_today is not None:
        taiex_series = [h for h in history.get(TAIEX_ID, []) if h["date"] != today]
        taiex_series.append({**taiex_today, "date": today, "volume": 0})
        taiex_series = taiex_series[-HISTORY_MAX_DAYS:]
        history[TAIEX_ID] = _attach_technical_indicators(taiex_series)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)

    return history


def _candle_body(c):
    return abs(c["close"] - c["open"])


def _is_bullish_candle(c):
    return c["close"] > c["open"]


def _upper_shadow(c):
    return c["high"] - max(c["open"], c["close"])


def _lower_shadow(c):
    return min(c["open"], c["close"]) - c["low"]


def _is_hammer_shape(c):
    """判斷K棒外型是否符合「錘子/吊人」的形狀：長下影線、小實體、幾乎沒有上影線"""
    total_range = c["high"] - c["low"]
    if total_range <= 0:
        return False
    body = _candle_body(c)
    lower = _lower_shadow(c)
    upper = _upper_shadow(c)
    return lower >= body * 2 and upper <= body * 0.5 and (body / total_range) < 0.4


def _trend_direction(series, window=5):
    """簡易趨勢判斷：目前收盤價 vs N天前收盤價"""
    if len(series) <= window:
        return None
    now_close = series[-1]["close"]
    past_close = series[-1 - window]["close"]
    if now_close > past_close:
        return "up"
    elif now_close < past_close:
        return "down"
    return None


def detect_candlestick_patterns(series: list) -> list:
    """
    偵測常見的K線型態（單根/雙根/三根K棒組合），回傳命中的型態文字清單。
    這些型態都是「機率性參考」，不保證未來走勢，建議搭配成交量、均線一起看。
    """
    hits = []
    if len(series) < 2:
        return hits

    trend = _trend_direction(series)
    today = series[-1]

    # 錘子線 / 吊人線（同樣的形狀，依趨勢位置決定意義）
    if _is_hammer_shape(today):
        if trend == "down":
            hits.append("錘子線（下跌後出現，偏多參考）")
        elif trend == "up":
            hits.append("吊人線（上漲後出現，偏空參考）")

    # 多頭吞噬 / 空頭吞噬
    if len(series) >= 2:
        prev = series[-2]
        prev_body_low = min(prev["open"], prev["close"])
        prev_body_high = max(prev["open"], prev["close"])
        today_body_low = min(today["open"], today["close"])
        today_body_high = max(today["open"], today["close"])

        engulfs = today_body_low <= prev_body_low and today_body_high >= prev_body_high
        if engulfs and not _is_bullish_candle(prev) and _is_bullish_candle(today) and trend == "down":
            hits.append("多頭吞噬（偏多參考）")
        elif engulfs and _is_bullish_candle(prev) and not _is_bullish_candle(today) and trend == "up":
            hits.append("空頭吞噬（偏空參考）")

    # 晨星 / 暮星（三根K棒組合）
    if len(series) >= 3:
        c1, c2, c3 = series[-3], series[-2], series[-1]
        c1_body = _candle_body(c1)
        c2_body = _candle_body(c2)
        c1_mid = (c1["open"] + c1["close"]) / 2

        # 晨星：長黑K -> 小實體(跳空向下) -> 長紅K收盤深入第一根實體內
        if (not _is_bullish_candle(c1) and c1_body > 0 and c2_body < c1_body * 0.4
                and max(c2["open"], c2["close"]) < c1["close"]
                and _is_bullish_candle(c3) and c3["close"] > c1_mid
                and trend == "down"):
            hits.append("晨星（三K棒組合，偏多參考）")

        # 暮星：長紅K -> 小實體(跳空向上) -> 長黑K收盤深入第一根實體內
        if (_is_bullish_candle(c1) and c1_body > 0 and c2_body < c1_body * 0.4
                and min(c2["open"], c2["close"]) > c1["close"]
                and not _is_bullish_candle(c3) and c3["close"] < c1_mid
                and trend == "up"):
            hits.append("暮星（三K棒組合，偏空參考）")

    return hits


def detect_chart_patterns(series: list, lookback: int = 40, tolerance: float = 0.03) -> list:
    """
    簡化版的雙重頂(M頭)/雙重底(W底)偵測。
    ⚠️ 這是簡化的高低點比對邏輯，不是嚴謹的圖形辨識演算法，
    準確度低於前面的K線組合型態，容易有誤判，僅供參考。
    頭肩頂/頭肩底這類需要判斷三個轉折點的型態，這裡沒有做，
    因為誤判機率更高，暫時不建議自動化判斷。
    """
    hits = []
    window = series[-lookback:] if len(series) > lookback else series
    if len(window) < 15:
        return hits

    closes = [h["close"] for h in window]
    highest = max(closes)
    highest_idx = closes.index(highest)
    lowest = min(closes)
    lowest_idx = closes.index(lowest)

    # 雙重頂（M頭）：找第二個接近前波高點的高點，且中間有明顯拉回
    for i in range(highest_idx + 5, len(closes)):
        if abs(closes[i] - highest) / highest <= tolerance:
            trough = min(closes[highest_idx:i]) if i > highest_idx else None
            if trough and (highest - trough) / highest >= 0.05:
                hits.append("M頭/雙重頂型態（簡化判斷，偏空參考）")
            break

    # 雙重底（W底）：找第二個接近前波低點的低點，且中間有明顯反彈
    for i in range(lowest_idx + 5, len(closes)):
        if lowest > 0 and abs(closes[i] - lowest) / lowest <= tolerance:
            peak = max(closes[lowest_idx:i]) if i > lowest_idx else None
            if peak and (peak - lowest) / lowest >= 0.05:
                hits.append("W底/雙重底型態（簡化判斷，偏多參考）")
            break

    return hits


def detect_divergence(series: list, indicator_key: str, indicator_name: str, lookback: int = 40, recent_window: int = 10) -> list:
    """
    簡化版的背離偵測（頂背離／底背離），比較「較早一段」跟「最近recent_window天」的價格高低點，
    以及對應的指標（RSI或MACD的DIF）數值：
    - 頂背離：最近的高點價格比較早的高點更高（創新高），但指標數值反而更低－ 動能減弱的警示，偏空參考
    - 底背離：最近的低點價格比較早的低點更低（創新低），但指標數值反而更高－ 動能減弱的警示，偏多參考
    背離通常被視為比單純的指標交叉更有參考價值的反轉警示訊號，因為它反映「價格創新高/低，
    但推動力道其實在減弱」，而不是單純兩條線交叉。

    ⚠️ 這是簡化的兩段高低點比對法，不是嚴謹的波段辨識演算法，容易受雜訊影響，僅供參考。
    """
    hits = []
    window = series[-lookback:] if len(series) > lookback else series
    n = len(window)
    if n < 20:
        return hits

    closes = [h["close"] for h in window]
    indicators = [h.get(indicator_key) for h in window]
    if any(v is None for v in indicators):
        return hits  # 資料不足（例如剛開始還沒累積夠天數算出RSI/MACD）時不判斷，避免誤判

    recent_start = n - recent_window
    earlier = closes[:recent_start]
    if len(earlier) < 5:
        return hits

    # 頂背離：較早段的最高點 vs 最近recent_window天的最高點
    earlier_peak_idx = closes.index(max(earlier))  # 用closes.index確保拿到window裡對應的絕對位置
    recent_peak_idx = recent_start + closes[recent_start:].index(max(closes[recent_start:]))
    if closes[recent_peak_idx] > closes[earlier_peak_idx] and indicators[recent_peak_idx] < indicators[earlier_peak_idx]:
        hits.append(f"{indicator_name}頂背離（簡化判斷，偏空參考）")

    # 底背離：較早段的最低點 vs 最近recent_window天的最低點
    earlier_trough_idx = closes.index(min(earlier))
    recent_trough_idx = recent_start + closes[recent_start:].index(min(closes[recent_start:]))
    if closes[recent_trough_idx] < closes[earlier_trough_idx] and indicators[recent_trough_idx] > indicators[earlier_trough_idx]:
        hits.append(f"{indicator_name}底背離（簡化判斷，偏多參考）")

    return hits


def _had_recent_opposite_cross(series: list, fast_key: str, slow_key: str, current_direction: str, lookback: int = 5) -> bool:
    """
    訊號確認期用：檢查同一組指標（例如ma5/ma20）在最近lookback天內（不含今天），
    是否已經出現過「方向相反」的交叉。如果有，代表這組指標最近訊號來回反覆
    （常見於盤整格局），這次的交叉可信度會打折扣，用來標註「近期訊號反覆」。
    current_direction: "黃金" 或 "死亡"（這次偵測到的交叉方向）
    """
    window = series[-(lookback + 2):-1]  # 不含今天，看今天以前最近lookback+1天
    if len(window) < 2:
        return False
    for i in range(1, len(window)):
        prev, cur = window[i - 1], window[i]
        if any(v is None for v in [prev.get(fast_key), prev.get(slow_key), cur.get(fast_key), cur.get(slow_key)]):
            continue
        if prev[fast_key] <= prev[slow_key] and cur[fast_key] > cur[slow_key]:
            cross_dir = "黃金"
        elif prev[fast_key] >= prev[slow_key] and cur[fast_key] < cur[slow_key]:
            cross_dir = "死亡"
        else:
            continue
        if cross_dir != current_direction:
            return True
    return False


def detect_buy_sell_signals(history: dict, watch_ids: list, breakout_window: int = 20):
    """
    針對監控股票，比對「今天 vs 昨天」的技術指標狀態，偵測以下訊號：
    - 均線交叉：MA5 穿越 MA20（黃金交叉＝買進參考／死亡交叉＝賣出參考）
    - KD交叉：K線穿越D線（黃金交叉＝買進參考／死亡交叉＝賣出參考）
    - MACD交叉：DIF穿越DEA信號線（黃金交叉＝買進參考／死亡交叉＝賣出參考）
    - RSI超買超賣：RSI由下往上穿越70（超買，偏空參考）／由上往下穿越30（超賣，偏多參考）
    - 支撐/壓力突破：今日收盤價突破近N日高點（偏多）或跌破近N日低點（偏空）
    - 背離：MACD／RSI背離（頂背離偏空參考／底背離偏多參考），比單純指標交叉更有反轉參考價值
    - 布林通道：股價觸及上軌（偏空參考）／觸及下軌（偏多參考），跟MA/KD/MACD這類「交叉型」邏輯不同，
      是看股價相對於自己近期波動範圍的位置；通道明顯收窄是中性資訊，代表近期波動縮小

    每個訊號都會加註是否有「放量確認」：業界常見標準是當日成交量達到
    近5日均量的1.5倍以上，視為放量，訊號可信度較高；沒放量則標示「量能未明顯放大」。

    訊號確認期：MA/KD/MACD這三組交叉訊號，會額外檢查「最近5天內是否已經出現過方向相反的交叉」，
    如果有，代表這組指標最近訊號來回反覆（常見於盤整格局），會標註「近期訊號反覆，可信度較低」。
    這類訊號一樣會完整顯示在⚡清單裡，但不會被算進 build_direction_suggestion 的建議分數。

    同時用 MA60（季線）判斷「趨勢過濾」：收盤價站上MA60視為多頭環境，跌破視為空頭環境。
    這裡只負責算出趨勢是多是空，實際「逆勢訊號不計入建議分數」的邏輯放在 build_direction_suggestion，
    這裡的訊號清單本身不會因為順逆勢而被過濾掉——逆勢訊號一樣完整顯示，只是計分時不採計。

    回傳 (signals, trend_by_code)：
    - signals：{ 股票代號: [訊號文字, ...] }，僅供參考，不構成投資建議
    - trend_by_code：{ 股票代號: "多" 或 "空" }，資料不足（不到60天）時該股票不會出現在裡面
    """
    VOLUME_CONFIRM_MULTIPLIER = 1.5

    signals = {}
    trend_by_code = {}
    for stock_id in watch_ids:
        series = history.get(stock_id, [])
        if len(series) < 2:
            continue

        today, yesterday = series[-1], series[-2]
        hits = []

        if today.get("ma60") is not None:
            trend_by_code[stock_id] = "多" if today["close"] >= today["ma60"] else "空"

        # 成交量確認：今日量 vs 前5日均量（不含今天）
        volume_note = ""
        prior_5 = series[-6:-1]
        today_volume = today.get("volume")
        if len(prior_5) == 5 and today_volume:
            avg5_volume = sum(h.get("volume", 0) for h in prior_5) / 5
            if avg5_volume > 0:
                if today_volume >= avg5_volume * VOLUME_CONFIRM_MULTIPLIER:
                    volume_note = "，有放量確認"
                else:
                    volume_note = "，量能未明顯放大"

        CONFIRM_LOOKBACK_DAYS = 5

        # 均線交叉
        if all(v is not None for v in [today.get("ma5"), today.get("ma20"), yesterday.get("ma5"), yesterday.get("ma20")]):
            if yesterday["ma5"] <= yesterday["ma20"] and today["ma5"] > today["ma20"]:
                whipsaw = "，近期訊號反覆，可信度較低" if _had_recent_opposite_cross(series, "ma5", "ma20", "黃金", CONFIRM_LOOKBACK_DAYS) else ""
                hits.append(f"MA黃金交叉（買進參考{volume_note}{whipsaw}）")
            elif yesterday["ma5"] >= yesterday["ma20"] and today["ma5"] < today["ma20"]:
                whipsaw = "，近期訊號反覆，可信度較低" if _had_recent_opposite_cross(series, "ma5", "ma20", "死亡", CONFIRM_LOOKBACK_DAYS) else ""
                hits.append(f"MA死亡交叉（賣出參考{volume_note}{whipsaw}）")

        # KD交叉
        if all(v is not None for v in [today.get("k"), today.get("d"), yesterday.get("k"), yesterday.get("d")]):
            if yesterday["k"] <= yesterday["d"] and today["k"] > today["d"]:
                whipsaw = "，近期訊號反覆，可信度較低" if _had_recent_opposite_cross(series, "k", "d", "黃金", CONFIRM_LOOKBACK_DAYS) else ""
                hits.append(f"KD黃金交叉（買進參考{volume_note}{whipsaw}）")
            elif yesterday["k"] >= yesterday["d"] and today["k"] < today["d"]:
                whipsaw = "，近期訊號反覆，可信度較低" if _had_recent_opposite_cross(series, "k", "d", "死亡", CONFIRM_LOOKBACK_DAYS) else ""
                hits.append(f"KD死亡交叉（賣出參考{volume_note}{whipsaw}）")

        # MACD交叉：DIF（快線-慢線）由下往上穿越 DEA（信號線）
        if all(v is not None for v in [today.get("dif"), today.get("dea"), yesterday.get("dif"), yesterday.get("dea")]):
            if yesterday["dif"] <= yesterday["dea"] and today["dif"] > today["dea"]:
                whipsaw = "，近期訊號反覆，可信度較低" if _had_recent_opposite_cross(series, "dif", "dea", "黃金", CONFIRM_LOOKBACK_DAYS) else ""
                hits.append(f"MACD黃金交叉（買進參考{volume_note}{whipsaw}）")
            elif yesterday["dif"] >= yesterday["dea"] and today["dif"] < today["dea"]:
                whipsaw = "，近期訊號反覆，可信度較低" if _had_recent_opposite_cross(series, "dif", "dea", "死亡", CONFIRM_LOOKBACK_DAYS) else ""
                hits.append(f"MACD死亡交叉（賣出參考{volume_note}{whipsaw}）")

        # RSI超買超賣：用「今天穿越門檻」的方式偵測（而不是每天只要RSI>70就一直重複提醒）
        RSI_OVERBOUGHT, RSI_OVERSOLD = 70, 30
        if today.get("rsi") is not None and yesterday.get("rsi") is not None:
            if yesterday["rsi"] < RSI_OVERBOUGHT <= today["rsi"]:
                hits.append("RSI超買（偏空參考，留意過熱回檔）")
            elif yesterday["rsi"] > RSI_OVERSOLD >= today["rsi"]:
                hits.append("RSI超賣（偏多參考，留意反彈機會）")

        # 支撐/壓力突破：用「今天以前」的近N日高低點來比對，避免用到今天自己的高低價
        prior = series[-(breakout_window + 1):-1]
        if len(prior) >= 5:  # 資料太少就不判斷，避免誤判
            recent_high = max(h["high"] for h in prior)
            recent_low = min(h["low"] for h in prior)
            if today["close"] > recent_high:
                hits.append(f"突破近{breakout_window}日壓力（偏多參考{volume_note}）")
            elif today["close"] < recent_low:
                hits.append(f"跌破近{breakout_window}日支撐（偏空參考{volume_note}）")

        # 布林通道：股價相對於自己近期正常波動範圍的位置，跟MA/KD/MACD這類「交叉型」訊號邏輯不同
        if today.get("bb_upper") is not None and today.get("bb_lower") is not None:
            if today["close"] > today["bb_upper"]:
                hits.append("布林通道觸及上軌（簡化判斷，偏空參考，短線可能過熱）")
            elif today["close"] < today["bb_lower"]:
                hits.append("布林通道觸及下軌（簡化判斷，偏多參考，短線可能超跌）")

            # 通道收窄：band寬度相對於中軌的比例，是近期20天內最窄的區間之一，代表最近波動明顯縮小
            # 這是中性、非方向性的資訊（歷史上通道收窄後常接著變盤，但不預設方向）
            band_width_pct = (today["bb_upper"] - today["bb_lower"]) / today["bb_middle"] if today["bb_middle"] else None
            recent_widths = [
                (h["bb_upper"] - h["bb_lower"]) / h["bb_middle"]
                for h in series[-20:]
                if h.get("bb_upper") is not None and h.get("bb_middle")
            ]
            if band_width_pct is not None and len(recent_widths) >= 10 and band_width_pct <= min(recent_widths):
                hits.append("布林通道明顯收窄（近期波動縮小，中性資訊，留意變盤）")

        # K線型態（錘子/吊人/吞噬/晨星暮星）+ 簡化版雙重頂底 + MACD/RSI背離
        hits.extend(detect_candlestick_patterns(series))
        hits.extend(detect_chart_patterns(series))
        hits.extend(detect_divergence(series, "rsi", "RSI"))
        hits.extend(detect_divergence(series, "dif", "MACD"))

        # 波動度基準：今日振幅（最高-最低）vs ATR，用來判斷今天的漲跌算不算「異常放大」
        # 這是中性、非方向性的資訊，不影響多空計分，純粹提供「今天波動是不是比平常劇烈」的參考
        if today.get("atr") and today["atr"] > 0:
            today_range = today["high"] - today["low"]
            ratio = today_range / today["atr"]
            if ratio >= 2:
                hits.append(f"今日振幅達近期正常波動的{round(ratio, 1)}倍（波動明顯放大，中性資訊，不代表方向）")

        if hits:
            signals[stock_id] = hits

    return signals, trend_by_code


def compute_simple_signals(market_df: pd.DataFrame, stock_ids: list):
    subset = market_df[market_df["證券代號"].astype(str).isin(stock_ids)]
    signals = []
    for _, row in subset.iterrows():
        signals.append({
            "代號": row["證券代號"],
            "名稱": row.get("證券名稱", ""),
            "收盤價": row["收盤價"],
            "漲跌幅%": round(row.get("漲跌幅%", 0), 2),
            "漲跌金額": round(row.get("漲跌價差", 0), 2),
        })
    return signals


# ============================================================
# 5. 推播層：訊息合併成一則，優先用 LINE，備援用 Telegram
# ============================================================

def shorten_signal_label(text: str) -> str:
    """
    把完整的訊號說明文字（含括號內的詳細參考說明，如「（簡化判斷，偏空參考）」）
    縮短成簡短標籤，只用於LINE推播，避免每檔股票的訊號列表在手機上佔用太多行。
    完整說明仍保留在網頁的「買賣訊號說明」對照表，資料本身（buy_sell_signals）不受影響。
    """
    import re
    short = text.split("（")[0]           # 拿掉括號內的說明文字
    short = re.sub(r"近\d+日", "", short)  # 「突破近20日壓力」->「突破壓力」
    short = short.split("/")[0]           # 「M頭/雙重頂型態」->「M頭」
    return short


def classify_signal_direction(text: str):
    """
    根據訊號文字裡的「偏多／偏空／買進／賣出」關鍵字，判斷這個訊號屬於多方還是空方訊號，
    用來加總計算「建議」方向。回傳 "多"、"空"、或 None（無法判斷方向的訊號，理論上不會出現）。
    """
    if ("偏多" in text) or ("買進" in text):
        return "多"
    if ("偏空" in text) or ("賣出" in text):
        return "空"
    return None


def classify_institutional_flow(code: str, inst_df: pd.DataFrame, keyword: str = "外資"):
    """
    查詢指定法人（外資／投信／自營商）當日買賣超方向，用來跟技術面訊號做交叉比對。
    keyword 可以是 "外資"、"投信"、"自營商"。
    有些法人的欄位在證交所資料裡會拆成好幾個子欄位（例如自營商拆成「自行買賣」「避險」），
    這裡會把符合關鍵字的「買賣超股數」欄位都加總起來，只用來判斷方向（正/負），不影響判斷結果。
    回傳 "買超"、"賣超"、或 None（沒有資料、找不到這檔股票、或加總後正好等於0）。
    """
    if inst_df is None or inst_df.empty or "證券代號" not in inst_df.columns:
        return None
    row_df = inst_df[inst_df["證券代號"].astype(str) == str(code)]
    if row_df.empty:
        return None
    row = row_df.iloc[0]

    total = 0.0
    found = False
    for col in inst_df.columns:
        if keyword in col and "買賣超股數" in col:
            try:
                total += float(str(row[col]).replace(",", ""))
                found = True
            except (ValueError, TypeError):
                continue
    if not found:
        return None
    if total > 0:
        return "買超"
    elif total < 0:
        return "賣超"
    return None


def classify_all_institutions_flow(code: str, inst_df: pd.DataFrame) -> dict:
    """同時查詢外資／投信／自營商三個法人當天的買賣超方向，回傳 {"外資":..., "投信":..., "自營商":...}"""
    return {
        "外資": classify_institutional_flow(code, inst_df, "外資"),
        "投信": classify_institutional_flow(code, inst_df, "投信"),
        "自營商": classify_institutional_flow(code, inst_df, "自營商"),
    }


def get_market_trend(history: dict):
    """
    用大盤指數（TAIEX）的收盤價 vs MA60，判斷目前整體大盤是多頭還是空頭環境。
    回傳 "多"、"空"、或 None（大盤歷史資料不足時，例如剛串接還沒累積夠60天）。
    """
    series = history.get(TAIEX_ID, [])
    if not series:
        return None
    today = series[-1]
    if today.get("ma60") is None:
        return None
    return "多" if today["close"] >= today["ma60"] else "空"


def build_market_alignment_note(direction: str, market_trend: str):
    """
    把個股技術面算出來的建議方向，跟大盤（加權指數）目前的多空環境做交叉比對。
    個股訊號跟大盤同方向時，代表這檔股票的走勢跟大環境一致，參考價值較高；
    個股訊號跟大盤方向相反時（例如大盤空頭但這檔股票逆勢出現多方訊號），
    不代表訊號是錯的，但屬於「逆勢個股」，波動風險可能較高，值得多留意，不是要你避開。
    ※ 僅供參考，不構成投資建議
    """
    if direction is None or market_trend is None:
        return None
    label = "多頭" if market_trend == "多" else "空頭"
    if direction == market_trend:
        return f"📊 大盤同步：目前大盤也是{label}環境，訊號跟大環境一致"
    else:
        return f"⚠️ 大盤逆勢：大盤目前是{label}環境，這檔個股訊號跟大環境不同，波動風險可能較高"


def build_cross_check_note(direction: str, flow_by_institution: dict):
    """
    把技術面訊號算出來的多空方向，跟外資／投信／自營商三大法人當日買賣超方向做交叉比對。
    要求「三大法人方向都跟技術面一致」才會標示「資金面支撐」，比只看單一法人更嚴謹保守；
    只要有任何一個法人方向跟技術面相反，就會提出警示；資料不完整時只列出目前確定知道的部分，
    不會勉強湊出「三大法人一致」的結論。
    這是「規則式比對」（多個獨立資料源方向是否一致），不是預測。
    ※ 僅供參考，不構成投資建議
    """
    if direction is None:
        return None

    target_flow = "買超" if direction == "多" else "賣超"
    opposite_flow = "賣超" if direction == "多" else "買超"

    known = {k: v for k, v in flow_by_institution.items() if v is not None}
    if not known:
        return None

    aligned = [k for k, v in known.items() if v == target_flow]
    opposed = [k for k, v in known.items() if v == opposite_flow]

    if opposed:
        return f"⚠️ 交叉比對：{'、'.join(opposed)}{opposite_flow}（與技術面方向不一致，僅供留意）"
    elif len(aligned) == 3:
        return f"🔍 交叉比對：三大法人同步{target_flow}（外資／投信／自營商一致），訊號有資金面支撐"
    elif aligned:
        return f"🔍 交叉比對：{'、'.join(aligned)}同步{target_flow}（其餘法人資料不足，僅供參考）"
    return None


def _signal_category(text: str) -> str:
    """
    把訊號文字歸類到對應的技術分析面向，用來做「跨面向交叉比對」。
    同一個面向裡出現好幾個訊號，本質上常常是同一件事的不同講法
    （例如同時出現MA黃金交叉又KD黃金交叉，都是動能轉強的跡象，關聯性很高，
    不該被當成兩個獨立證據）；真正有意義的交叉驗證，是看「不同面向」
    是否都指向同一個方向。
    """
    if "背離" in text:
        return "反轉背離"
    if any(k in text for k in ["錘子線", "吊人線", "吞噬", "晨星", "暮星", "雙重頂", "雙重底"]):
        return "K線型態"
    if "布林通道" in text:
        return "波動通道"
    if "RSI超買" in text or "RSI超賣" in text:
        return "動能極值"
    if "突破" in text or "跌破" in text:
        return "價格突破"
    if any(k in text for k in ["MA黃金", "MA死亡", "KD黃金", "KD死亡", "MACD黃金", "MACD死亡"]):
        return "趨勢動能交叉"
    return "其他"


def _signal_weight(text: str) -> float:
    """
    幫每種訊號類型分配一個權重，用在計分時取代原本「每個訊號都算1分」的簡單加總。
    這是根據技術分析常見的可信度分級做的粗略調整，不是精確的統計驗證結果，僅供參考：
    - 背離（頂/底背離）：權重較高（1.5），業界普遍認為比單純交叉更有反轉參考價值
    - 有放量確認的訊號：權重較高（1.3），成交量佐證讓訊號更可信
    - 單根/組合K棒型態（錘子、吊人、吞噬、晨星、暮星）：權重較低（0.7），容易受單日雜訊影響
    - 其餘（一般MA/KD/MACD交叉、無放量確認的突破/跌破、RSI超買超賣）：標準權重（1.0）
    """
    if "背離" in text:
        return 1.5
    if "有放量確認" in text:
        return 1.3
    if any(k in text for k in ["錘子線", "吊人線", "吞噬", "晨星", "暮星"]):
        return 0.7
    return 1.0


def build_direction_suggestion(signals: list, trend=None):
    """
    把同一檔股票當天的技術訊號，分兩層算出一個「建議」標籤：

    第一層（面向內計分）：先用 _signal_category() 把訊號分成幾個技術面向
    （趨勢動能交叉／動能極值／K線型態／反轉背離／價格突破／波動通道），
    同一面向內用 _signal_weight() 加權計分，得出這個面向整體是偏多、偏空、還是不明確。
    這一層是為了避免「同一件事講很多遍」被誤認成很多獨立證據——
    例如MA黃金交叉+KD黃金交叉本質上都是動能轉強的同一種訊息，不該疊加成兩倍份量。

    第二層（跨面向交叉比對）：看有幾個「不同面向」彼此獨立地指向同一個方向。
    一致的面向數越多，代表這個判斷是被多個不同角度交叉驗證過的，不是只靠單一指標，
    參考價值通常比單一面向的訊號更高，用「強烈／中等／初步」標註這個差異。

    這整套仍然是「規則式比對」，不是AI生成的分析，多空面向數相同時如實呈現「意見分歧」，
    不會硬湊出一個方向。

    trend（"多"或"空"，來自MA60趨勢判斷）：如果有提供，「逆勢」訊號不會被計入分數
    （例如目前站上MA60的多頭環境下出現的偏空訊號，計分時會被排除），
    但訊號本身仍完整顯示在⚡清單裡，不會被隱藏——只是不會拿來當作建議依據。

    同樣的道理，標註「近期訊號反覆，可信度較低」的訊號（訊號確認期判斷出來的）
    也不會被計入分數。「今日振幅異常」「布林通道收窄」這類中性、非方向性的訊號，
    本來就不含方向關鍵字，天然不會被計入。

    回傳 (建議文字, 方向)：
    - 方向是 "多"、"空"、或 None（沒有方向性面向、或面向數打平時為 None，代表無法用來做交叉比對）
    - 建議文字沒有東西可顯示時為 None
    ※ 僅供參考，不構成投資建議
    """
    category_scores = defaultdict(lambda: {"多": 0.0, "空": 0.0})
    excluded = 0
    for s in signals:
        d = classify_signal_direction(s)
        if d is None:
            continue
        if "訊號反覆" in s:
            excluded += 1
            continue
        if trend and d != trend:
            excluded += 1
            continue
        category_scores[_signal_category(s)][d] += _signal_weight(s)

    category_direction = {}
    for cat, score in category_scores.items():
        if score["多"] > score["空"]:
            category_direction[cat] = "多"
        elif score["空"] > score["多"]:
            category_direction[cat] = "空"
        # 面向內部剛好打平的情況很少見，這種面向不計入方向，但不影響其他面向判斷

    if not category_direction:
        return None, None

    trend_note = ""
    if trend:
        trend_label = "多頭環境（站上MA60）" if trend == "多" else "空頭環境（跌破MA60）"
        trend_note = f"，目前{trend_label}"
    if excluded:
        trend_note += f"，{excluded}項逆勢或訊號反覆的項目未計入"

    bull_categories = [c for c, d in category_direction.items() if d == "多"]
    bear_categories = [c for c, d in category_direction.items() if d == "空"]

    if len(bull_categories) == len(bear_categories):
        cats_text = "、".join(sorted(category_direction.keys()))
        return f"⚖️ 各面向訊號不一致（{cats_text}意見分歧），建議觀望{trend_note}", None

    if len(bull_categories) > len(bear_categories):
        direction, agree, oppose = "多", bull_categories, bear_categories
    else:
        direction, agree, oppose = "空", bear_categories, bull_categories

    icon = "📈" if direction == "多" else "📉"
    label = "偏多" if direction == "多" else "偏空"
    if len(agree) >= 3 and not oppose:
        strength = "🔥強烈"
    elif len(agree) >= 2:
        strength = "中等"
    else:
        strength = "初步"
    oppose_note = f"，但{'、'.join(oppose)}面向相反" if oppose else ""

    return f"{icon} {strength}建議{label}（{len(agree)}個獨立面向一致：{'、'.join(agree)}{oppose_note}{trend_note}）", direction


DIVIDER = "━━━━━━━━━━━━━━"


def format_message(core_signals, dynamic_watchlist, market_df, buy_sell_signals=None, trend_by_code=None, inst_df=None, market_trend=None):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📊 台股每日監控　{today}", DIVIDER]

    lines.append("【核心自選股：有買進／賣出訊號】")
    shown_count = 0
    for s in core_signals:
        code = str(s['代號'])
        full_signals = (buy_sell_signals or {}).get(code, [])
        trend = (trend_by_code or {}).get(code)
        suggestion, direction = build_direction_suggestion(full_signals, trend)
        if direction is None:
            continue  # 沒有明確買進/賣出方向（沒訊號、或各面向意見分歧）的股票不顯示

        shown_count += 1
        chg_amount = s.get('漲跌金額', 0)
        sign = "+" if chg_amount >= 0 else ""
        dot = "🔴" if chg_amount >= 0 else "🟢"  # 跟網頁K線圖同色系：紅漲綠跌
        line = f"{dot} {code} {s['名稱']}　{s['收盤價']}　{sign}{chg_amount}（{sign}{s['漲跌幅%']}%）"
        short_labels = [shorten_signal_label(sig) for sig in full_signals]
        line += "\n　　⚡ " + "、".join(short_labels)
        line += f"\n　　{suggestion}"
        flow_by_institution = classify_all_institutions_flow(code, inst_df)
        cross_note = build_cross_check_note(direction, flow_by_institution)
        if cross_note:
            line += f"\n　　{cross_note}"
        market_note = build_market_alignment_note(direction, market_trend)
        if market_note:
            line += f"\n　　{market_note}"
        lines.append(line)

    if shown_count == 0:
        lines.append("目前核心自選股沒有明確的買進／賣出訊號")

    lookup = market_df.set_index(market_df["證券代號"].astype(str))
    for category, ids in dynamic_watchlist.items():
        if not ids:
            continue
        top_ids = ids[:PUSH_MESSAGE_TOP_N]
        lines.append("")
        lines.append(DIVIDER)
        lines.append(f"【{category}】命中 {len(ids)} 檔，以下為前{len(top_ids)}：")
        for stock_id in top_ids:
            if stock_id not in lookup.index:
                continue
            row = lookup.loc[stock_id]
            name = row.get("證券名稱", "")
            price = row.get("收盤價", "")
            chg = round(row.get("漲跌幅%", 0), 2)
            chg_amount = round(row.get("漲跌價差", 0), 2)
            sign = "+" if chg_amount >= 0 else ""
            dot = "🔴" if chg_amount >= 0 else "🟢"
            lines.append(f"{dot} {stock_id} {name}　{price}（{sign}{chg}%）")

    lines.append("")
    lines.append(DIVIDER)
    lines.append("※ 以上訊號僅供參考，不構成投資建議")

    return "\n".join(lines)


def send_line_message(text: str) -> bool:
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_TARGET_ID:
        print("LINE推播略過：LINE_CHANNEL_ACCESS_TOKEN 或 LINE_TARGET_ID 未設定")
        return False
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {"to": LINE_TARGET_ID, "messages": [{"type": "text", "text": text}]}
    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
    if resp.status_code != 200:
        # 印出LINE官方回傳的錯誤內容（如「Not found」「The user hasn't added the bot as a friend」等），
        # 這樣在 GitHub Actions 的 log 裡就能直接看到真正卡在哪，不用用猜的
        print(f"LINE推播失敗，status={resp.status_code}，回應內容：{resp.text}")
    return resp.status_code == 200


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    resp = requests.post(url, data=payload, timeout=10)
    return resp.status_code == 200


def push_message(text: str):
    """
    統一的推播入口：如果 SKIP_PUSH=true（手動測試時可以設定），
    只會印出訊息內容到log，不會真的發送LINE/Telegram，避免測試浪費推播額度。
    """
    if SKIP_PUSH:
        print("【測試模式，未實際推播】")
        return
    sent = send_line_message(text)
    if sent:
        print("LINE推播成功")
    else:
        print("LINE推播失敗，改嘗試Telegram")
        sent = send_telegram_message(text)
        if sent:
            print("Telegram推播成功")
        else:
            print("Telegram推播也失敗（或未設定），這則訊息沒有送出")


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
        otc_df = fetch_otc_market_daily()
        if not otc_df.empty:
            market_df = pd.concat([market_df, otc_df], ignore_index=True)
    except Exception as e:
        print(f"合併上櫃股票資料失敗，本次僅使用上市股票資料：{e}")
    try:
        inst_df = fetch_institutional_investors()
    except Exception:
        inst_df = pd.DataFrame()

    # 股票代號 -> 名稱對照表，供投資論點追蹤、催化事件追蹤等區塊顯示股票名稱用
    stock_name_lookup = dict(zip(
        market_df["證券代號"].astype(str), market_df["證券名稱"].astype(str)
    )) if "證券代號" in market_df.columns and "證券名稱" in market_df.columns else {}

    dynamic_watchlist = build_dynamic_watchlist(market_df, inst_df)
    core_signals = compute_simple_signals(market_df, watch_ids)

    # 6.3 更新 OHLC 歷史 + 技術指標（MA5/MA20/KD）
    # 追蹤範圍擴大到「核心自選股 + 動態清單當日命中的股票」，這樣動態清單的股票之後才有機會累積出走勢圖
    # （只算買賣訊號時，還是只看 watch_ids，避免每天變動的動態股票被誤判交叉訊號）
    all_dynamic_ids = [sid for ids in dynamic_watchlist.values() for sid in ids]
    history_ids = list(dict.fromkeys(watch_ids + all_dynamic_ids))
    history = update_price_history(market_df, history_ids)
    buy_sell_signals, trend_by_code = detect_buy_sell_signals(history, watch_ids)
    with open(os.path.join(DATA_DIR, "signals.json"), "w", encoding="utf-8") as f:
        json.dump(buy_sell_signals, f, ensure_ascii=False)

    # 6.4 推播（核心自選股 + 使用者額外關注的股票，都會被推播提醒，含買賣訊號）
    market_trend = get_market_trend(history)
    message = format_message(core_signals, dynamic_watchlist, market_df, buy_sell_signals, trend_by_code, inst_df, market_trend)
    print(message)
    push_message(message)

    # 6.5 存檔：全市場備份（放 docs/data 底下，供之後擴充查詢用）
    today_str = datetime.now().strftime("%Y%m%d")
    market_df.to_csv(os.path.join(DATA_DIR, f"market_{today_str}.csv"), index=False, encoding="utf-8-sig")

    # 6.6 儀表板專用簡化格式
    write_dashboard_csv(market_df, core_signals, dynamic_watchlist)

    # 6.7 今日新聞（重大訊息公告）
    # news_df 提到 try 區塊外宣告，是因為 6.9 催化事件追蹤要重複使用這份資料
    news_df = pd.DataFrame()
    try:
        news_df = fetch_material_announcements()
        news_df.to_csv(os.path.join(DATA_DIR, "news.csv"), index=False, encoding="utf-8-sig")
    except Exception as e:
        print(f"抓取重大訊息公告失敗，略過本次新聞更新：{e}")

    # 6.8 投資論點維護（規則式比對，只針對 INVESTMENT_THESIS 裡有設定的股票）
    if INVESTMENT_THESIS:
        try:
            monthly_revenue = fetch_monthly_revenue(list(INVESTMENT_THESIS.keys()))
            thesis_results = check_investment_thesis(monthly_revenue, stock_name_lookup)
            with open(os.path.join(DATA_DIR, "thesis_tracking.json"), "w", encoding="utf-8") as f:
                json.dump(thesis_results, f, ensure_ascii=False)

            # 只有「低於預期」的論點才會額外推播提醒，避免每天都塞一堆正常訊息
            alerts = [r for r in thesis_results if "低於預期" in r["狀態"]]
            if alerts:
                lines = ["📌 投資論點提醒：以下持股可能需要重新檢視", ""]
                for r in alerts:
                    lines.append(f"{r['代號']} {r.get('名稱','')}：{r['狀態']}")
                    if r["備註"]:
                        lines.append(f"　　原始論點：{r['備註']}")
                lines.append("")
                lines.append("※ 此為規則式比對，非AI生成分析，僅供參考，不構成投資建議")
                push_message("\n".join(lines))
        except Exception as e:
            print(f"投資論點比對失敗，略過本次更新：{e}")

    # 6.9 催化事件追蹤（股東會日期 + 法說會關鍵字比對，全部免費資料源）
    try:
        catalyst_events = build_catalyst_events(CATALYST_WATCHLIST, news_df, stock_name_lookup)
        with open(os.path.join(DATA_DIR, "catalyst_events.json"), "w", encoding="utf-8") as f:
            json.dump(catalyst_events, f, ensure_ascii=False)

        # 只推播「還沒推播過」且日期落在提醒天數內的事件，避免每次執行都重複轟炸
        notified_path = os.path.join(DATA_DIR, "catalyst_notified.json")
        notified = set()
        if os.path.exists(notified_path):
            with open(notified_path, "r", encoding="utf-8") as f:
                notified = set(json.load(f))

        today = datetime.now()
        new_alerts = []
        still_notified = set(notified)
        for e in catalyst_events:
            if not e["日期"]:
                continue
            key = f"{e['代號']}_{e['類型']}_{e['日期']}"
            if key in notified:
                continue
            try:
                y, m, d = e["日期"].split("/")
                days_left = (datetime(int(y) + 1911, int(m), int(d)) - today).days
            except Exception:
                continue
            if 0 <= days_left <= CATALYST_ALERT_DAYS_AHEAD:
                new_alerts.append((e, days_left))
                still_notified.add(key)

        if new_alerts:
            lines = ["📅 催化事件提醒", DIVIDER]
            for e, days_left in new_alerts:
                icon = "📌" if e["類型"] == "股東會" else "🎤"
                lines.append(f"{icon} {e['代號']} {e.get('名稱','')}　{e['類型']}　{e['日期']}（{days_left}天後）")
            lines.append("")
            lines.append(DIVIDER)
            lines.append("※ 股東會日期來自證交所結構化資料；法說會日期為重大訊息公告關鍵字比對，僅供參考，不構成投資建議")
            push_message("\n".join(lines))

        with open(notified_path, "w", encoding="utf-8") as f:
            json.dump(list(still_notified), f, ensure_ascii=False)
    except Exception as e:
        print(f"催化事件追蹤失敗，略過本次更新：{e}")


def write_dashboard_csv(market_df, core_signals, dynamic_watchlist):
    """
    產生 docs/data/latest.csv，欄位固定為：
    證券代號,證券名稱,收盤價,漲跌幅%,漲跌金額,最高價,最低價,成交股數,成交金額,類別
    """
    rows = []
    lookup = market_df.set_index(market_df["證券代號"].astype(str))

    for s in core_signals:
        code = str(s["代號"])
        market_row = lookup.loc[code] if code in lookup.index else {}
        rows.append({
            "證券代號": s["代號"],
            "證券名稱": s["名稱"],
            "收盤價": s["收盤價"],
            "漲跌幅%": s["漲跌幅%"],
            "漲跌金額": s.get("漲跌金額", 0),
            "最高價": market_row.get("最高價", "") if len(market_row) else "",
            "最低價": market_row.get("最低價", "") if len(market_row) else "",
            "成交股數": market_row.get("成交股數", "") if len(market_row) else "",
            "成交金額": market_row.get("成交金額", "") if len(market_row) else "",
            "類別": "核心自選股",
        })

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
                "漲跌金額": round(row.get("漲跌價差", 0), 2),
                "最高價": row.get("最高價", ""),
                "最低價": row.get("最低價", ""),
                "成交股數": row.get("成交股數", ""),
                "成交金額": row.get("成交金額", ""),
                "類別": category,
            })

    pd.DataFrame(rows).to_csv(os.path.join(DATA_DIR, "latest.csv"), index=False, encoding="utf-8-sig")


def fetch_realtime_quotes(stock_ids: list) -> dict:
    """
    抓取盤中即時報價（免費，證交所基本市況報導網站，非官方文件化的內部端點，
    穩定性不像 OpenAPI 那麼有保障，若失敗會回傳空字典，呼叫端要自行處理）。
    只查詢傳入的股票代號，不涵蓋全市場，所以只適合用在「核心自選股/關注股票」這種數量有限的清單。
    回傳 { 代號: {"name":..., "price":..., "chg_pct":..., "high":..., "low":...} }
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
            continue  # 尚未成交或非交易時段，沒有最新成交價
        try:
            prev_close = float(item.get("y"))
        except (TypeError, ValueError):
            prev_close = None
        chg_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0
        chg_amount = round(price - prev_close, 2) if prev_close else 0

        # 當日至今的盤中最高/最低，KD跟支撐壓力判斷會用到；抓不到就先用目前價格頂替
        try:
            high = float(item.get("h"))
        except (TypeError, ValueError):
            high = price
        try:
            low = float(item.get("l"))
        except (TypeError, ValueError):
            low = price

        quotes[code] = {
            "name": item.get("n", ""), "price": price, "chg_pct": chg_pct, "chg_amount": chg_amount,
            "high": high, "low": low,
        }
    return quotes


def detect_intraday_signals(quotes: dict, history: dict, watch_ids: list, breakout_window: int = 20) -> dict:
    """
    盤中訊號偵測：把「目前即時價格」當作假設的今日收盤價，重新算一次MA5/MA20/KD，
    跟歷史資料裡「昨天收盤時算出的數值」比對，看有沒有觸發交叉或突破。

    ⚠️ 這些是「暫定」訊號，因為股價在收盤前還可能變動，跟收盤後正式算出來的結果不一定一樣，
    純粹是提早讓你知道「目前這個時間點看起來像是要交叉了」，還是要等收盤確認。
    """
    signals = {}
    for stock_id in watch_ids:
        series = history.get(stock_id, [])
        q = quotes.get(stock_id)
        if not series or not q:
            continue

        prev = series[-1]  # 昨天收盤算出的指標值
        closes = [h["close"] for h in series] + [q["price"]]
        highs = [h["high"] for h in series] + [q["high"]]
        lows = [h["low"] for h in series] + [q["low"]]

        today_ma5 = compute_ma(closes, 5)[-1]
        today_ma20 = compute_ma(closes, 20)[-1]
        today_k, today_d = compute_kd(highs, lows, closes)
        today_k, today_d = today_k[-1], today_d[-1]

        hits = []

        if all(v is not None for v in [today_ma5, today_ma20, prev.get("ma5"), prev.get("ma20")]):
            if prev["ma5"] <= prev["ma20"] and today_ma5 > today_ma20:
                hits.append("MA黃金交叉（盤中暫定，買進參考）")
            elif prev["ma5"] >= prev["ma20"] and today_ma5 < today_ma20:
                hits.append("MA死亡交叉（盤中暫定，賣出參考）")

        if all(v is not None for v in [today_k, today_d, prev.get("k"), prev.get("d")]):
            if prev["k"] <= prev["d"] and today_k > today_d:
                hits.append("KD黃金交叉（盤中暫定，買進參考）")
            elif prev["k"] >= prev["d"] and today_k < today_d:
                hits.append("KD死亡交叉（盤中暫定，賣出參考）")

        prior = series[-breakout_window:]
        if len(prior) >= 5:
            recent_high = max(h["high"] for h in prior)
            recent_low = min(h["low"] for h in prior)
            if q["price"] > recent_high:
                hits.append(f"突破近{breakout_window}日壓力（盤中暫定，偏多參考）")
            elif q["price"] < recent_low:
                hits.append(f"跌破近{breakout_window}日支撐（盤中暫定，偏空參考）")

        if hits:
            signals[stock_id] = hits

    return signals


def intraday_main():
    """
    盤中即時更新（另外排程，一天2次：10:00 / 12:00）：
    - 更新「核心自選股 + 關注股票」的即時價格
    - 用即時價格 + 歷史資料，偵測盤中「暫定」買賣訊號
    - 不重新計算價格異常/成交量異常/熱度潛力股（那些需要全市場收盤資料，維持一天一次）
    - 一樣走 LINE 推播，失敗自動改用 Telegram 備援
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    extra_watchlist = load_extra_watchlist()
    watch_ids = list(dict.fromkeys(CORE_WATCHLIST + extra_watchlist))

    quotes = fetch_realtime_quotes(watch_ids)
    if not quotes:
        print("盤中即時報價抓取失敗或無資料，略過本次更新")
        return

    history_path = os.path.join(DATA_DIR, HISTORY_FILE_NAME)
    history = {}
    if os.path.exists(history_path):
        with open(history_path, "r", encoding="utf-8") as f:
            history = json.load(f)
    intraday_signals = detect_intraday_signals(quotes, history, watch_ids)

    now = datetime.now().strftime("%H:%M")
    lines = [f"⏱ 盤中即時報價 {now}", ""]
    for code in watch_ids:
        q = quotes.get(code)
        if not q:
            continue
        sign = "+" if q["chg_amount"] >= 0 else ""
        line = f"{code} {q['name']}：{q['price']}（{sign}{q['chg_amount']} / {q['chg_pct']}%）"
        if intraday_signals.get(code):
            line += "\n　　⚡ " + "、".join(intraday_signals[code])
        lines.append(line)
    lines.append("")
    lines.append("※ 盤中訊號為暫定值，以收盤後正式結果為準，僅供參考，不構成投資建議")
    message = "\n".join(lines)
    print(message)
    push_message(message)

    # 更新 latest.csv 裡「核心自選股」部分的價格，動態分類(價格異常等)維持前一次收盤後的資料不變
    latest_path = os.path.join(DATA_DIR, "latest.csv")
    if os.path.exists(latest_path):
        df = pd.read_csv(latest_path)
    else:
        df = pd.DataFrame(columns=["證券代號", "證券名稱", "收盤價", "漲跌幅%", "漲跌金額", "成交股數", "類別"])

    for code in watch_ids:
        q = quotes.get(code)
        if not q:
            continue
        mask = (df["證券代號"].astype(str) == code) & (df["類別"] == "核心自選股")
        if mask.any():
            df.loc[mask, "收盤價"] = q["price"]
            df.loc[mask, "漲跌幅%"] = q["chg_pct"]
            df.loc[mask, "漲跌金額"] = q["chg_amount"]
        else:
            new_row = pd.DataFrame([{
                "證券代號": code, "證券名稱": q["name"], "收盤價": q["price"],
                "漲跌幅%": q["chg_pct"], "漲跌金額": q["chg_amount"], "成交股數": "", "類別": "核心自選股",
            }])
            df = pd.concat([df, new_row], ignore_index=True)

    df.to_csv(latest_path, index=False, encoding="utf-8-sig")

    # 盤中訊號也存一份給網頁用，跟收盤後的signals.json分開，避免互相覆蓋
    with open(os.path.join(DATA_DIR, "intraday_signals.json"), "w", encoding="utf-8") as f:
        json.dump(intraday_signals, f, ensure_ascii=False)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "intraday":
        intraday_main()
    else:
        main()
