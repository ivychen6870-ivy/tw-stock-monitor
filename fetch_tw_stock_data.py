# -*- coding: utf-8 -*-
"""
fetch_tw_stock_data.py

從 FinMind API 抓取台股每日資料，整理成 tw_stock_indicators.py 需要的格式。

【重要】這個檔案沒辦法在 Claude 這邊執行測試，因為這裡的網路權限沒有開放給
股市資料網站。請把這個檔案放進你的 GitHub Actions repo，跟 tw_stock_indicators.py
放在一起，在那邊實際呼叫、測試。

安裝需求：
    pip install requests pandas

FinMind API 文件：https://finmind.github.io/
免費額度：未註冊 300 次/小時，註冊並驗證信箱後帶 token 可提高到 600 次/小時
（token 申請：https://finmindtrade.com/ 註冊後在會員頁面取得）

使用方式：
    from fetch_tw_stock_data import fetch_full_dataset
    from tw_stock_indicators import generate_signals

    df = fetch_full_dataset(stock_id="2330", start_date="2025-01-01", token="你的token")
    result = generate_signals(df)
    print(result[["close", "score", "decision"]].tail())
"""

import sys
from datetime import datetime
import requests
import pandas as pd

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


def _finmind_request(dataset: str, stock_id: str = None, start_date: str = None,
                      end_date: str = None, token: str = "") -> pd.DataFrame:
    """呼叫 FinMind API 的共用函式，回傳整理好的 DataFrame（尚未設定 index）"""
    params = {"dataset": dataset}
    if stock_id:
        params["data_id"] = stock_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(FINMIND_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    if "data" not in payload:
        raise RuntimeError(f"FinMind API 回傳格式異常: {payload}")

    return pd.DataFrame(payload["data"])


def fetch_price_data(stock_id: str, start_date: str, end_date: str = None,
                      token: str = "", adjusted: bool = True) -> pd.DataFrame:
    """
    抓取每日股價 OHLCV。
    adjusted=True（預設）：用「台灣還原股價資料表」（TaiwanStockPriceAdj），
        除權息已經還原調整過，MA/KD/MACD這類需要連續價格序列的指標不會被除權息
        造成的價格跳空誤判成假的交叉訊號。技術分析用途建議一律用還原股價。
    adjusted=False：用原始股價（TaiwanStockPrice），沒有做除權息還原。

    【重要】TaiwanStockPriceAdj 必須同時帶 start_date 和 end_date 才能正常查詢，
    只帶 start_date 會被拒絕（400錯誤，已實測確認）。這裡固定會補上 end_date
    （沒指定就預設是今天），呼叫端不用自己處理。

    【重要】指數（例如加權指數 data_id="001"）沒有除權息的概念，
    TaiwanStockPriceAdj 不支援指數代號，查指數時請把 adjusted 設成 False，
    改用一般的 TaiwanStockPrice。

    回傳 index 為日期、欄位為 open/high/low/close/volume 的 DataFrame，
    格式直接對應 tw_stock_indicators.py 的輸入需求。

    注意：興櫃個股的 open 欄位是「前日均價」而非當日開盤價（FinMind 官方文件說明），
    可能出現 open 落在當日高低點之外的情形，非資料錯誤，使用上市櫃股票不受影響。
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    dataset = "TaiwanStockPriceAdj" if adjusted else "TaiwanStockPrice"
    df = _finmind_request(dataset, stock_id, start_date, end_date, token)

    if df.empty:
        raise ValueError(f"查無股價資料：stock_id={stock_id}, start_date={start_date}, dataset={dataset}")

    df = df.rename(columns={
        "max": "high",
        "min": "low",
        "Trading_Volume": "volume",
    })

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    return df[["open", "high", "low", "close", "volume"]]


def fetch_index_price(data_id: str, start_date: str, end_date: str = None,
                       token: str = "") -> pd.DataFrame:
    """
    抓取指數（例如加權指數 data_id="001"）的每日OHLC，一律用未還原股價
    （TaiwanStockPriceAdj 不支援指數代號，指數本身也沒有除權息還原的概念）。
    格式跟 fetch_price_data() 一致。
    """
    return fetch_price_data(data_id, start_date, end_date, token, adjusted=False)


def fetch_institutional_investors(stock_id: str, start_date: str, end_date: str = None,
                                   token: str = "") -> pd.DataFrame:
    """
    抓取三大法人（外資、投信、自營商）每日買賣超
    （資料集：TaiwanStockInstitutionalInvestorsBuySell）

    回傳整理過的「淨買超」欄位（買進-賣出），可用於籌碼面交叉驗證：
        foreign_net   外資淨買超
        trust_net     投信淨買超
        dealer_net    自營商淨買超（自行買賣+避險合計）
        total_net     三大法人合計淨買超
    """
    df = _finmind_request("TaiwanStockInstitutionalInvestorsBuySell",
                           stock_id, start_date, end_date, token)

    if df.empty:
        return pd.DataFrame(columns=["foreign_net", "trust_net", "dealer_net", "total_net"])

    df["net"] = df["buy"] - df["sell"]
    df["date"] = pd.to_datetime(df["date"])

    pivot = df.pivot_table(index="date", columns="name", values="net", aggfunc="sum").fillna(0)

    # FinMind 法人名稱對照（依官方文件命名，如遇欄位對不上請對照最新 API 回傳的 name 值調整）
    rename_map = {
        "Foreign_Investor": "foreign_net",
        "Investment_Trust": "trust_net",
        "Dealer_self": "dealer_self_net",
        "Dealer_Hedging": "dealer_hedging_net",
    }
    pivot = pivot.rename(columns=rename_map)

    dealer_cols = [c for c in ["dealer_self_net", "dealer_hedging_net"] if c in pivot.columns]
    if dealer_cols:
        pivot["dealer_net"] = pivot[dealer_cols].sum(axis=1)

    keep_cols = [c for c in ["foreign_net", "trust_net", "dealer_net"] if c in pivot.columns]
    pivot = pivot[keep_cols]
    pivot["total_net"] = pivot[keep_cols].sum(axis=1) if keep_cols else 0

    return pivot.sort_index()


def fetch_futures_daily(futures_id: str = "TX", start_date: str = None, end_date: str = None,
                         token: str = "", near_month_only: bool = True,
                         session: str = "day") -> pd.DataFrame:
    """
    抓取期貨每日資料（資料集：TaiwanFuturesDaily），預設抓台指期（TX）。
    常用期貨代碼：TX（台指期）、MTX（小台指）、TE（電子期）、TF（金融期）

    session 參數決定抓哪個交易時段，三者是分開計算技術指標用的：
        "day"   日盤（08:45-13:45），對應 trading_session = "position"
        "night" 夜盤（15:00-隔日05:00），對應 trading_session = "after_market"
        "both"  日盤+夜盤直接接續合併成一條連續序列（近似「盤中一直交易」的連續走勢）

    回傳 index 為日期，欄位為 open/high/low/close/volume/settlement_price/open_interest，
    格式跟 fetch_price_data() 一致，可直接餵給 tw_stock_indicators.generate_signals()。
    日盤、夜盤請分開呼叫兩次（session="day" 一次、session="night" 一次），
    各自算出的技術指標分數才不會互相混在一起。

    near_month_only=True（預設）：每個交易日只留「近月合約」（最常被當作台指期指標的那一口），
    避免同一天有近月、遠月多筆資料混在一起。
    """
    df = _finmind_request("TaiwanFuturesDaily", futures_id, start_date, end_date, token)

    if df.empty:
        raise ValueError(f"查無期貨資料：futures_id={futures_id}, start_date={start_date}")

    if "trading_session" in df.columns:
        if session == "day":
            df = df[df["trading_session"] == "position"]
        elif session == "night":
            df = df[df["trading_session"] == "after_market"]
        # session == "both" 時不過濾，日盤+夜盤都保留，依日期排序後自然接續

    df["date"] = pd.to_datetime(df["date"])

    if near_month_only and "contract_date" in df.columns:
        nearest = df.groupby("date")["contract_date"].transform("min")
        df = df[df["contract_date"] == nearest]

    df = df.rename(columns={"max": "high", "min": "low"})
    df = df.sort_values(["date", "trading_session"]) if "trading_session" in df.columns else df.sort_values("date")
    df = df.set_index("date")

    keep_cols = [c for c in ["open", "high", "low", "close", "volume",
                              "settlement_price", "open_interest"] if c in df.columns]
    return df[keep_cols]


def fetch_futures_snapshot(futures_id: str = "TX1", token: str = "") -> dict:
    """
    抓取期貨近即時報價（約30秒更新一次），適合做「盤中即時大盤/台指期」顯示用。
    資料集：taiwan_futures_snapshot（注意這個是獨立端點，不是走 dataset 參數那套）
    futures_id 用近月合約代碼，例如 TX1（台指期近月）、MTX1（小台指近月）

    回傳單一合約的最新快照（dict），欄位包含 close/change_price/change_rate/volume 等。
    盤後時段呼叫可能會拿到最後一筆收盤時的快照，非錯誤。
    """
    url = "https://api.finmindtrade.com/api/v4/taiwan_futures_snapshot"
    params = {"data_id": futures_id}
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    data = payload.get("data", [])
    if not data:
        raise ValueError(f"查無期貨即時報價：futures_id={futures_id}")

    return data[0]


def fetch_full_dataset(stock_id: str, start_date: str, end_date: str = None,
                        token: str = "") -> pd.DataFrame:
    """
    一次抓齊「股價」+「三大法人買賣」，合併成單一 DataFrame。
    可直接餵給 tw_stock_indicators.generate_signals()
    （法人欄位不影響現有計分邏輯，只是附加資訊；等你要做籌碼面交叉驗證時再取用）
    """
    price_df = fetch_price_data(stock_id, start_date, end_date, token)
    inst_df = fetch_institutional_investors(stock_id, start_date, end_date, token)

    merged = price_df.join(inst_df, how="left")
    return merged


if __name__ == "__main__":
    # 使用範例，需要在有網路權限、可連到 FinMind 的環境執行（GitHub Actions 或本機）
    STOCK_ID = "2330"          # 範例：台積電，換成你要追蹤的股票代碼
    START_DATE = "2025-01-01"
    TOKEN = ""                 # 有註冊 FinMind 的話填入你的 token，可以提高請求上限

    try:
        df = fetch_full_dataset(STOCK_ID, START_DATE, token=TOKEN)
        print(df.tail(10))

        futures_day = fetch_futures_daily("TX", START_DATE, token=TOKEN, session="day")
        print("\n=== 台指期日盤（近月）===")
        print(futures_day.tail(5))

        futures_night = fetch_futures_daily("TX", START_DATE, token=TOKEN, session="night")
        print("\n=== 台指期夜盤（近月）===")
        print(futures_night.tail(5))

        # 日盤、夜盤各自套用技術指標計分，範例：
        # from tw_stock_indicators import generate_signals
        # day_signals = generate_signals(futures_day)
        # night_signals = generate_signals(futures_night)

    except Exception as e:
        print(f"抓取資料失敗: {e}", file=sys.stderr)
        sys.exit(1)
