# -*- coding: utf-8 -*-
"""
tw_stock_indicators.py

台股技術分析多重指標模組
------------------------
純 pandas / numpy 實作，不依賴任何外部股市 API，
可直接複製到 GitHub Actions 的 repo 中，搭配你原本的資料抓取流程使用。

輸入資料格式：
    df 需為 DataFrame，index 為日期（由舊到新排序），
    欄位至少包含： open, high, low, close, volume

使用方式：
    from tw_stock_indicators import generate_signals
    result = generate_signals(df)
    print(result[["close", "score", "decision"]].tail())
"""

import numpy as np
import pandas as pd
import statistics


# ============================================================
# 基礎技術指標
# ============================================================

def calc_ma(df: pd.DataFrame, period: int = 60, column: str = "close") -> pd.Series:
    """移動平均線 MA"""
    return df[column].rolling(window=period).mean()


def calc_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26,
              signal: int = 9, column: str = "close") -> pd.DataFrame:
    """
    MACD
    回傳欄位: macd, signal, histogram
    """
    ema_fast = df[column].ewm(span=fast, adjust=False).mean()
    ema_slow = df[column].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram,
    })


def calc_rsi(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    """RSI"""
    delta = df[column].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)  # 資料不足時給中性值，避免下游判斷出錯


def calc_kd(df: pd.DataFrame, period: int = 9, k_smooth: int = 3,
            d_smooth: int = 3) -> pd.DataFrame:
    """KD (Stochastic Oscillator)，需要 high/low/close"""
    low_min = df["low"].rolling(window=period).min()
    high_max = df["high"].rolling(window=period).max()
    rsv = (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(com=(k_smooth - 1), adjust=False).mean()
    d = k.ewm(com=(d_smooth - 1), adjust=False).mean()
    return pd.DataFrame({"k": k, "d": d})


def calc_bias(df: pd.DataFrame, period: int = 24, column: str = "close") -> pd.Series:
    """乖離率 BIAS = (收盤 - MA) / MA * 100"""
    ma = df[column].rolling(window=period).mean()
    return (df[column] - ma) / ma * 100


def calc_bollinger(df: pd.DataFrame, period: int = 20, num_std: float = 2.0,
                    column: str = "close") -> pd.DataFrame:
    """布林通道，回傳 upper/mid/lower/bandwidth（%）"""
    mid = df[column].rolling(window=period).mean()
    std = df[column].rolling(window=period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    bandwidth = (upper - lower) / mid * 100
    return pd.DataFrame({"upper": upper, "mid": mid, "lower": lower, "bandwidth": bandwidth})


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    ATR（真實波動幅度均值）：衡量近期波動大小，波動越大 ATR 越高。
    用來動態調整停損寬度：波動大時停損拉寬，波動小時停損收窄，
    避免用同一個百分比停損套用在所有股票/所有時期。
    """
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def calc_dmi_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """DMI (+DI / -DI) 與 ADX 趨勢強度，需要 high/low/close"""
    high, low, close = df["high"], df["low"], df["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()

    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr

    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()

    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx.fillna(0)})


# ============================================================
# 均線交叉 / KD交叉 / 突破支撐壓力
# ============================================================

def calc_ma_cross(df: pd.DataFrame, fast: int = 5, slow: int = 20,
                   column: str = "close") -> pd.DataFrame:
    """
    MA 黃金交叉 / 死亡交叉
    MA5 由下往上穿越 MA20 → golden_cross
    MA5 由上往下穿越 MA20 → death_cross
    """
    ma_fast = df[column].rolling(window=fast).mean()
    ma_slow = df[column].rolling(window=slow).mean()
    prev_diff = (ma_fast - ma_slow).shift(1)
    curr_diff = ma_fast - ma_slow

    golden_cross = (prev_diff < 0) & (curr_diff > 0)
    death_cross = (prev_diff > 0) & (curr_diff < 0)

    return pd.DataFrame({
        "ma_fast": ma_fast,
        "ma_slow": ma_slow,
        "ma_golden_cross": golden_cross,
        "ma_death_cross": death_cross,
    })


def calc_kd_cross(k: pd.Series, d: pd.Series) -> pd.DataFrame:
    """
    KD 黃金交叉 / 死亡交叉，並依所在高低檔區給不同權重
    K<20 出現黃金交叉 → 權重 1.5（低檔訊號較可靠）
    K>80 出現死亡交叉 → 權重 1.5（高檔訊號較可靠）
    其餘一般交叉 → 權重 1.0
    """
    prev_diff = (k - d).shift(1)
    curr_diff = k - d

    golden_cross = (prev_diff < 0) & (curr_diff > 0)
    death_cross = (prev_diff > 0) & (curr_diff < 0)

    golden_weight = np.where(golden_cross & (k < 20), 1.5,
                              np.where(golden_cross, 1.0, 0.0))
    death_weight = np.where(death_cross & (k > 80), 1.5,
                             np.where(death_cross, 1.0, 0.0))

    return pd.DataFrame({
        "kd_golden_cross": golden_cross,
        "kd_death_cross": death_cross,
        "kd_golden_weight": golden_weight,
        "kd_death_weight": death_weight,
    })


def calc_breakout(df: pd.DataFrame, period: int = 20,
                   vol_multiplier: float = 1.5) -> pd.DataFrame:
    """
    突破近 N 日壓力 / 跌破近 N 日支撐，並用成交量做確認。
    量增標準：當日量 > 近 N 日均量 * vol_multiplier（常用標準抓 1.5 倍）
    量有確認 → 權重 1.0；量沒放大（假突破疑慮）→ 權重 0.5
    """
    resistance = df["high"].rolling(window=period).max().shift(1)
    support = df["low"].rolling(window=period).min().shift(1)
    avg_vol = df["volume"].rolling(window=period).mean()

    breakout_up = df["close"] > resistance
    breakout_down = df["close"] < support
    vol_confirmed = df["volume"] > (avg_vol * vol_multiplier)

    breakout_up_weight = np.where(breakout_up & vol_confirmed, 1.0,
                                   np.where(breakout_up, 0.5, 0.0))
    breakout_down_weight = np.where(breakout_down & vol_confirmed, 1.0,
                                     np.where(breakout_down, 0.5, 0.0))

    return pd.DataFrame({
        "resistance_20d": resistance,
        "support_20d": support,
        "breakout_up": breakout_up,
        "breakout_down": breakout_down,
        "breakout_up_weight": breakout_up_weight,
        "breakout_down_weight": breakout_down_weight,
    })


# ============================================================
# K棒型態辨識
# ============================================================

def detect_hammer_hanging_man(df: pd.DataFrame, trend_window: int = 5,
                               body_ratio: float = 0.3,
                               shadow_ratio: float = 2.0) -> pd.DataFrame:
    """
    錘子線 / 吊人線：外型相同（長下影線、小實體），用出現前的趨勢方向區分。
    先前處於下跌趨勢 → 錘子線（偏多，探底）
    先前處於上漲趨勢 → 吊人線（偏空，轉弱）
    """
    body = (df["close"] - df["open"]).abs()
    lower_shadow = df[["open", "close"]].min(axis=1) - df["low"]
    upper_shadow = df["high"] - df[["open", "close"]].max(axis=1)
    candle_range = (df["high"] - df["low"]).replace(0, np.nan)

    body_safe = body.where(body != 0, candle_range * 0.05)
    is_small_body = body <= candle_range * body_ratio
    is_long_lower_shadow = lower_shadow >= body_safe * shadow_ratio
    is_small_upper_shadow = upper_shadow <= body_safe * 0.5

    shape_match = is_small_body & is_long_lower_shadow & is_small_upper_shadow

    prior_trend = df["close"].diff(trend_window)
    hammer = shape_match & (prior_trend < 0)
    hanging_man = shape_match & (prior_trend > 0)

    return pd.DataFrame({"hammer": hammer.fillna(False), "hanging_man": hanging_man.fillna(False)})


def detect_engulfing(df: pd.DataFrame) -> pd.DataFrame:
    """多頭吞噬 / 空頭吞噬：今日實體完全包覆前一日實體，且顏色相反"""
    o, c = df["open"], df["close"]
    prev_o, prev_c = df["open"].shift(1), df["close"].shift(1)

    bullish_engulfing = (c > o) & (prev_c < prev_o) & (c >= prev_o) & (o <= prev_c)
    bearish_engulfing = (c < o) & (prev_c > prev_o) & (o >= prev_c) & (c <= prev_o)

    return pd.DataFrame({
        "bullish_engulfing": bullish_engulfing.fillna(False),
        "bearish_engulfing": bearish_engulfing.fillna(False),
    })


def detect_morning_evening_star(df: pd.DataFrame, long_body_ratio: float = 0.6,
                                 small_body_ratio: float = 0.3) -> pd.DataFrame:
    """
    晨星（底部反轉，偏多）/ 暮星（頭部反轉，偏空）
    三根K棒組合：長黑(紅) → 小實體 → 長紅(黑)且收深入第一根實體一半以上
    """
    o0, c0 = df["open"].shift(2), df["close"].shift(2)
    h0, l0 = df["high"].shift(2), df["low"].shift(2)
    o1, c1 = df["open"].shift(1), df["close"].shift(1)
    h1, l1 = df["high"].shift(1), df["low"].shift(1)
    o2, c2 = df["open"], df["close"]

    body0 = c0 - o0
    range0 = (h0 - l0).replace(0, np.nan)
    body1_abs = (c1 - o1).abs()
    range1 = (h1 - l1).replace(0, np.nan)

    long_black0 = (body0 < 0) & (body0.abs() >= range0 * long_body_ratio)
    long_red0 = (body0 > 0) & (body0.abs() >= range0 * long_body_ratio)
    small_body1 = body1_abs <= range1 * small_body_ratio

    morning_star = (
        long_black0 & small_body1
        & (c2 > o2)
        & (c2 >= o0 + body0.abs() * 0.5)
    )
    evening_star = (
        long_red0 & small_body1
        & (c2 < o2)
        & (c2 <= o0 - body0.abs() * 0.5)
    )

    return pd.DataFrame({
        "morning_star": morning_star.fillna(False),
        "evening_star": evening_star.fillna(False),
    })


def detect_double_top_bottom(df: pd.DataFrame, order: int = 10,
                              tolerance: float = 0.02) -> pd.DataFrame:
    """
    M頭/雙重頂、W底/雙重底（簡化版，準確度較低，建議給較低權重）
    抓最近兩個局部高點（低點），高度相近（誤差在 tolerance 內）就視為雙重頂（底）
    """
    price = df["close"]
    double_top = pd.Series(False, index=df.index)
    double_bottom = pd.Series(False, index=df.index)

    peaks = _local_extrema(price, order, "max").dropna()
    troughs = _local_extrema(price, order, "min").dropna()

    peak_idx = peaks.index
    for i in range(1, len(peak_idx)):
        prev_i, curr_i = peak_idx[i - 1], peak_idx[i]
        if abs(price[curr_i] - price[prev_i]) / price[prev_i] <= tolerance:
            double_top[curr_i] = True

    trough_idx = troughs.index
    for i in range(1, len(trough_idx)):
        prev_i, curr_i = trough_idx[i - 1], trough_idx[i]
        if abs(price[curr_i] - price[prev_i]) / price[prev_i] <= tolerance:
            double_bottom[curr_i] = True

    return pd.DataFrame({"double_top": double_top, "double_bottom": double_bottom})


# ============================================================
# 背離偵測 / 洗盤(whipsaw)偵測
# ============================================================

def _local_extrema(series: pd.Series, order: int, mode: str = "max") -> pd.Series:
    """找出局部極值點（前後 order 根K棒內的最大/最小值）"""
    window = order * 2 + 1
    if mode == "max":
        rolled = series.rolling(window=window, center=True).max()
    else:
        rolled = series.rolling(window=window, center=True).min()
    return series[series == rolled]


def detect_divergence(df: pd.DataFrame, indicator_col: str, price_col: str = "close",
                       order: int = 5) -> pd.Series:
    """
    背離偵測（頂背離 / 底背離）
    比較最近兩個價格波峰(谷)與對應指標波峰(谷)的方向是否一致。

    回傳 Series，值為 'bearish'（頂背離：價格創高但指標未創高，警示賣出）、
                       'bullish'（底背離：價格破底但指標未破底，警示買進）、
                       或 NaN（無訊號）
    """
    price = df[price_col]
    indicator = df[indicator_col]

    result = pd.Series(index=df.index, dtype=object)

    peaks = _local_extrema(price, order, "max").dropna()
    troughs = _local_extrema(price, order, "min").dropna()

    # 頂背離：價格後高於前高，但指標後高低於前高
    peak_idx = peaks.index
    for i in range(1, len(peak_idx)):
        prev_i, curr_i = peak_idx[i - 1], peak_idx[i]
        if price[curr_i] > price[prev_i] and indicator[curr_i] < indicator[prev_i]:
            result[curr_i] = "bearish"

    # 底背離：價格後低於前低，但指標後低高於前低
    trough_idx = troughs.index
    for i in range(1, len(trough_idx)):
        prev_i, curr_i = trough_idx[i - 1], trough_idx[i]
        if price[curr_i] < price[prev_i] and indicator[curr_i] > indicator[prev_i]:
            result[curr_i] = "bullish"

    return result


def detect_whipsaw(signal_series: pd.Series, lookback: int = 5, max_flips: int = 3) -> pd.Series:
    """
    洗盤/假突破偵測：短期內訊號方向反覆翻轉過於頻繁時標記為 True，
    代表當前訊號可信度低，建議觀望。

    signal_series: 例如 np.sign(macd_hist) 或 K-D 這類方向性訊號（1 / -1 / 0）
    """
    flips = signal_series.diff().fillna(0) != 0
    flip_count = flips.rolling(window=lookback).sum()
    return flip_count >= max_flips


# ============================================================
# 綜合買賣訊號判定
# ============================================================

# 各型態訊號的權重，之後想調整可以直接改這裡，不用動計分邏輯
PATTERN_WEIGHT_DOUBLE_TOP_BOTTOM = 0.5  # 準確度較低，先給較低權重


def generate_signals(df: pd.DataFrame, buy_threshold: float = 8,
                      sell_threshold: float = -8) -> pd.DataFrame:
    """
    整合全部指標並產生綜合評分與買賣判定。

    基礎指標（每個貢獻 -1 / 0 / +1）：
        MACD 柱狀圖   > 0 偏多 / < 0 偏空
        RSI           < 30 偏多（超賣）/ > 70 偏空（超買）
        KD            K > D 偏多 / K < D 偏空
        MA60          收盤價站上 MA60 偏多 / 跌破偏空
        BIAS          乖離 < -5% 偏多（超跌） / > +5% 偏空（超漲）
        布林通道       觸及下軌偏多 / 觸及上軌偏空
        DMI/ADX       僅在 ADX > 20（趨勢確立）時，依 +DI / -DI 方向計分

    型態訊號（新增，每個貢獻 -1 / 0 / +1，除非另有註明）：
        MA5/20 交叉    黃金交叉 +1 / 死亡交叉 -1
        KD 交叉        一般 ±1，低檔黃金交叉(K<20)或高檔死亡交叉(K>80) 加碼到 ±1.5
        突破壓力/支撐   量增確認(量>20日均量1.5倍) ±1，量沒放大 ±0.5
        錘子線/吊人線   ±1
        吞噬型態       ±1
        晨星/暮星      ±1
        雙重頂/雙重底   ±0.5（準確度較低，權重調低）

    背離、洗盤兩個機制維持原本設計：
        背離：底背離加分、頂背離扣分
        洗盤：短期訊號反覆翻轉時，總分打對折

    decision 欄位：score >= buy_threshold → 買進
                   score <= sell_threshold → 賣出
                   其餘 → 觀望

    buy_threshold / sell_threshold 預設調整為 ±8（原本7個指標是±4，
    現在指標數量變多，總分上限也跟著提高，維持大約同樣的「多數指標同向」比例）
    """
    result = df.copy()

    macd_df = calc_macd(df)
    result["macd"] = macd_df["macd"]
    result["macd_signal"] = macd_df["signal"]
    result["macd_hist"] = macd_df["histogram"]

    result["rsi"] = calc_rsi(df)

    kd_df = calc_kd(df)
    result["k"] = kd_df["k"]
    result["d"] = kd_df["d"]

    result["ma60"] = calc_ma(df, 60)
    result["bias"] = calc_bias(df)

    boll_df = calc_bollinger(df)
    result["boll_upper"] = boll_df["upper"]
    result["boll_mid"] = boll_df["mid"]
    result["boll_lower"] = boll_df["lower"]

    dmi_df = calc_dmi_adx(df)
    result["plus_di"] = dmi_df["plus_di"]
    result["minus_di"] = dmi_df["minus_di"]
    result["adx"] = dmi_df["adx"]

    result["atr"] = calc_atr(df)

    ma_cross_df = calc_ma_cross(df)
    result["ma5"] = ma_cross_df["ma_fast"]
    result["ma20"] = ma_cross_df["ma_slow"]
    result["ma_golden_cross"] = ma_cross_df["ma_golden_cross"]
    result["ma_death_cross"] = ma_cross_df["ma_death_cross"]

    kd_cross_df = calc_kd_cross(result["k"], result["d"])
    result["kd_golden_cross"] = kd_cross_df["kd_golden_cross"]
    result["kd_death_cross"] = kd_cross_df["kd_death_cross"]

    breakout_df = calc_breakout(df)
    result["breakout_up"] = breakout_df["breakout_up"]
    result["breakout_down"] = breakout_df["breakout_down"]
    result["resistance_20d"] = breakout_df["resistance_20d"]
    result["support_20d"] = breakout_df["support_20d"]

    hammer_df = detect_hammer_hanging_man(df)
    result["hammer"] = hammer_df["hammer"]
    result["hanging_man"] = hammer_df["hanging_man"]

    engulfing_df = detect_engulfing(df)
    result["bullish_engulfing"] = engulfing_df["bullish_engulfing"]
    result["bearish_engulfing"] = engulfing_df["bearish_engulfing"]

    star_df = detect_morning_evening_star(df)
    result["morning_star"] = star_df["morning_star"]
    result["evening_star"] = star_df["evening_star"]

    double_df = detect_double_top_bottom(df)
    result["double_top"] = double_df["double_top"]
    result["double_bottom"] = double_df["double_bottom"]

    result["divergence_rsi"] = detect_divergence(result, indicator_col="rsi")
    result["whipsaw"] = detect_whipsaw(np.sign(result["macd_hist"]))

    score = pd.Series(0.0, index=df.index)

    # --- 基礎指標 ---
    score += np.sign(result["macd_hist"]).fillna(0)
    score += np.where(result["rsi"] < 30, 1, np.where(result["rsi"] > 70, -1, 0))
    score += np.where(result["k"] > result["d"], 1, -1)
    score += np.where(df["close"] > result["ma60"], 1, -1)
    score += np.where(result["bias"] < -5, 1, np.where(result["bias"] > 5, -1, 0))
    score += np.where(df["close"] <= result["boll_lower"], 1,
                       np.where(df["close"] >= result["boll_upper"], -1, 0))
    trend_dir = np.where(result["plus_di"] > result["minus_di"], 1, -1)
    score += np.where(result["adx"] > 20, trend_dir, 0)

    # --- 均線/KD交叉、突破支撐壓力 ---
    score += np.where(result["ma_golden_cross"], 1, np.where(result["ma_death_cross"], -1, 0))
    score += kd_cross_df["kd_golden_weight"] - kd_cross_df["kd_death_weight"]
    score += breakout_df["breakout_up_weight"] - breakout_df["breakout_down_weight"]

    # --- K棒型態 ---
    score += np.where(result["hammer"], 1, np.where(result["hanging_man"], -1, 0))
    score += np.where(result["bullish_engulfing"], 1, np.where(result["bearish_engulfing"], -1, 0))
    score += np.where(result["morning_star"], 1, np.where(result["evening_star"], -1, 0))
    score += np.where(result["double_bottom"], PATTERN_WEIGHT_DOUBLE_TOP_BOTTOM,
                       np.where(result["double_top"], -PATTERN_WEIGHT_DOUBLE_TOP_BOTTOM, 0))

    # --- 背離加分/扣分 ---
    score += np.where(result["divergence_rsi"] == "bullish", 1,
                       np.where(result["divergence_rsi"] == "bearish", -1, 0))

    # --- 洗盤警示：若訊號近期反覆翻轉，評分打折避免誤判 ---
    score = np.where(result["whipsaw"], score * 0.5, score)

    result["score"] = score
    result["decision"] = np.select(
        [result["score"] >= buy_threshold, result["score"] <= sell_threshold],
        ["買進", "賣出"],
        default="觀望",
    )

    return result


# ============================================================
# 進場/停損/停利價位計算
# ============================================================

def calculate_price_levels(df: pd.DataFrame, result: pd.DataFrame,
                            atr_multiplier: float = 1.5, rr_ratio: float = 3.0) -> pd.DataFrame:
    """
    在 generate_signals() 的結果上，針對「買進」「賣出」判定的那幾天，
    額外算出具體的進場/停損/停利價位。「觀望」的日子這幾欄會是 NaN。

    這些數字是用客觀公式算出來的參考價位，不是對未來價格的預測，
    使用前請自行評估，不構成投資建議。

    停損（兩種算法都給，方便你自己比較）：
        stop_loss_atr      ATR 動態停損：進場價 ± atr_multiplier 倍 ATR
                            （波動大時停損拉寬，波動小時停損收窄）
        stop_loss_support  支撐/壓力停損：買進用近20日低點，賣出用近20日高點
                            （價格跌破近期支撐，或漲破近期壓力，代表原判斷可能不成立）

    停利/目標價（兩種算法都給）：
        take_profit_rr          固定風報比：以 ATR 停損的風險距離 × rr_ratio 往獲利方向推算
                                 （目前 rr_ratio=3，即冒1塊風險博3塊獲利）
        take_profit_resistance  下一個壓力/支撐位：買進看近20日高點或布林上軌（取較高者）；
                                 賣出看近20日低點或布林下軌（取較低者）

    參數：
        atr_multiplier  ATR 停損的倍數，預設 1.5
        rr_ratio        固定風報比的比例，預設 3.0（1:3）
    """
    levels = pd.DataFrame(index=result.index)

    is_buy = result["decision"] == "買進"
    is_sell = result["decision"] == "賣出"

    entry = df["close"]
    atr = result["atr"]

    # --- 買進：停損在下方，停利在上方 ---
    buy_stop_atr = entry - atr_multiplier * atr
    buy_stop_support = result["support_20d"]
    buy_risk = entry - buy_stop_atr
    buy_target_rr = entry + rr_ratio * buy_risk
    buy_target_resistance = pd.concat([result["resistance_20d"], result["boll_upper"]], axis=1).max(axis=1)

    # --- 賣出：停損在上方，停利在下方 ---
    sell_stop_atr = entry + atr_multiplier * atr
    sell_stop_support = result["resistance_20d"]
    sell_risk = sell_stop_atr - entry
    sell_target_rr = entry - rr_ratio * sell_risk
    sell_target_resistance = pd.concat([result["support_20d"], result["boll_lower"]], axis=1).min(axis=1)

    levels["entry_price"] = np.where(is_buy | is_sell, entry, np.nan)
    levels["stop_loss_atr"] = np.where(is_buy, buy_stop_atr, np.where(is_sell, sell_stop_atr, np.nan))
    levels["stop_loss_support"] = np.where(is_buy, buy_stop_support, np.where(is_sell, sell_stop_support, np.nan))
    levels["take_profit_rr"] = np.where(is_buy, buy_target_rr, np.where(is_sell, sell_target_rr, np.nan))
    levels["take_profit_resistance"] = np.where(
        is_buy, buy_target_resistance, np.where(is_sell, sell_target_resistance, np.nan)
    )

    return levels


# ============================================================
# 訊號回測：統計歷史上訊號發生後 N 天的報酬表現
# ============================================================

def backtest_signal_returns(df: pd.DataFrame, result: pd.DataFrame,
                             horizons=(63, 126), min_sample_warn: int = 10) -> dict:
    """
    回測歷史上「買進」「賣出」訊號發生後，往後 N 個交易日的報酬率統計。

    【重要】這是統計「過去發生過什麼」，不是預測「未來會發生什麼」。
    過去績效不代表未來表現，樣本數太少時統計本身也不可靠，請務必參考
    每組結果裡的 count（樣本數）跟 note（樣本不足提示）。

    horizons 預設 (63, 126)：63個交易日≈3個月，126個交易日≈半年
    （台股一年約240-245個交易日，一個月約20-21個交易日）

    報酬率計算方式：
        買進訊號：direction=+1，報酬 = (N天後收盤價 - 訊號當天收盤價) / 訊號當天收盤價
        賣出訊號：direction=-1，報酬 = -1 × 上面同樣的漲跌幅
                  （賣出訊號代表看空，所以股價下跌才是訊號「對」，報酬算正的）

    回傳格式：
        {
          63: {
            "買進": {
                "count": 樣本數,
                "mean_return_pct": 平均報酬率(%),
                "median_return_pct": 中位數報酬率(%),
                "win_rate_pct": 勝率(%，N天後報酬>0的比例),
                "min_return_pct": 最差報酬率(%),
                "max_return_pct": 最佳報酬率(%),
                "note": 樣本數不足時的提醒（可能不存在這個key）,
                "details": [ {signal_date, entry_price, exit_date, exit_price, return_pct}, ... ]
            },
            "賣出": {...}
          },
          126: {...}
        }
    """
    closes = df["close"]
    n = len(df)
    output = {}

    for horizon in horizons:
        horizon_result = {}
        for decision_label, direction in [("買進", 1), ("賣出", -1)]:
            signal_dates = result.index[result["decision"] == decision_label]
            details = []

            for d in signal_dates:
                pos = df.index.get_loc(d)
                if pos + horizon >= n:
                    continue  # 這個訊號發生的時間點太靠近資料尾端，還沒有足夠的未來資料可以算

                entry_price = float(closes.iloc[pos])
                exit_price = float(closes.iloc[pos + horizon])
                raw_return = (exit_price - entry_price) / entry_price
                signal_return_pct = round(direction * raw_return * 100, 2)

                details.append({
                    "signal_date": d,
                    "entry_price": round(entry_price, 2),
                    "exit_date": df.index[pos + horizon],
                    "exit_price": round(exit_price, 2),
                    "return_pct": signal_return_pct,
                })

            if details:
                returns = [x["return_pct"] for x in details]
                stat = {
                    "count": len(details),
                    "mean_return_pct": round(sum(returns) / len(returns), 2),
                    "median_return_pct": round(statistics.median(returns), 2),
                    "win_rate_pct": round(sum(1 for r in returns if r > 0) / len(returns) * 100, 1),
                    "min_return_pct": min(returns),
                    "max_return_pct": max(returns),
                    "details": details,
                }
                if len(details) < min_sample_warn:
                    stat["note"] = f"樣本數只有{len(details)}次，少於{min_sample_warn}次，統計意義有限，僅供參考"
            else:
                stat = {
                    "count": 0,
                    "note": "歷史上沒有出現過這個訊號，或資料長度不夠長、無法算出這個時間窗的未來報酬",
                    "details": [],
                }

            horizon_result[decision_label] = stat

        output[horizon] = horizon_result

    return output


# ============================================================
# 假資料自我測試（僅用於在 Claude 這邊驗證邏輯，不會呼叫任何外部 API）
# ============================================================

if __name__ == "__main__":
    rng = np.random.default_rng(42)
    n = 150
    dates = pd.date_range("2025-12-01", periods=n, freq="B")

    close = 100 + np.cumsum(rng.normal(0, 1.2, n))
    high = close + rng.uniform(0.2, 1.5, n)
    low = close - rng.uniform(0.2, 1.5, n)
    open_ = close + rng.normal(0, 0.5, n)
    volume = rng.integers(1000, 8000, n)

    df = pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    }, index=dates)

    result = generate_signals(df)

    print("=== 最近 10 筆綜合判定 ===")
    print(result[["close", "rsi", "adx", "score", "decision"]].tail(10).round(2))

    print("\n=== decision 分布 ===")
    print(result["decision"].value_counts())

    print("\n=== 背離訊號筆數 ===")
    print(result["divergence_rsi"].value_counts(dropna=True))

    print("\n=== 洗盤警示筆數 ===")
    print(result["whipsaw"].sum())

    pattern_cols = [
        "ma_golden_cross", "ma_death_cross",
        "kd_golden_cross", "kd_death_cross",
        "breakout_up", "breakout_down",
        "hammer", "hanging_man",
        "bullish_engulfing", "bearish_engulfing",
        "morning_star", "evening_star",
        "double_top", "double_bottom",
    ]
    print("\n=== 各型態訊號觸發次數 ===")
    print(result[pattern_cols].sum())

    levels = calculate_price_levels(df, result)
    combined = pd.concat([result[["close", "decision"]], levels], axis=1)
    trade_rows = combined[combined["decision"] != "觀望"]
    print("\n=== 買進/賣出訊號的進場停損停利價位（最近幾筆）===")
    print(trade_rows.tail(8).round(2))

    # --- 回測demo：用更長的假資料 + 較低門檻，才有足夠樣本數可以展示 ---
    print("\n" + "=" * 60)
    print("回測 demo（用較長假資料+較低門檻，純粹展示回測邏輯是否正確）")
    print("=" * 60)

    n_long = 800
    dates_long = pd.date_range("2023-01-01", periods=n_long, freq="B")
    close_long = 100 + np.cumsum(rng.normal(0, 1.2, n_long))
    high_long = close_long + rng.uniform(0.2, 1.5, n_long)
    low_long = close_long - rng.uniform(0.2, 1.5, n_long)
    open_long = close_long + rng.normal(0, 0.5, n_long)
    volume_long = rng.integers(1000, 8000, n_long)
    df_long = pd.DataFrame({
        "open": open_long, "high": high_long, "low": low_long,
        "close": close_long, "volume": volume_long,
    }, index=dates_long)

    result_long = generate_signals(df_long, buy_threshold=3, sell_threshold=-3)
    backtest = backtest_signal_returns(df_long, result_long, horizons=(63, 126))

    for horizon, label in [(63, "3個月(63交易日)"), (126, "半年(126交易日)")]:
        print(f"\n--- {label} ---")
        for decision_label in ["買進", "賣出"]:
            stat = backtest[horizon][decision_label]
            print(f"[{decision_label}] 樣本數={stat['count']}", end="")
            if stat["count"] > 0:
                print(f", 平均={stat['mean_return_pct']}%, 中位數={stat['median_return_pct']}%, "
                      f"勝率={stat['win_rate_pct']}%, 最差={stat['min_return_pct']}%, 最佳={stat['max_return_pct']}%")
            else:
                print(f" — {stat['note']}")
            if stat.get("note") and stat["count"] > 0:
                print(f"  提醒：{stat['note']}")
