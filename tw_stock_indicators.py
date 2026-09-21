"""
tw_stock_indicators.py
台股技術指標計算與買賣訊號評分模組
"""

import numpy as np
import pandas as pd


# ============================================================
# 常數
# ============================================================
PATTERN_WEIGHT_DOUBLE_TOP_BOTTOM = 0.3  # 雙重頂/雙重底型態權重
VOLUME_CONFIRM_MULTIPLIER = 1.5         # 量能確認倍數（20日均量）


# ============================================================
# 基礎指標計算
# ============================================================
def calc_ma(df):
    result = df.copy()
    for period in (5, 10, 20, 60, 120, 240):
        result[f"ma{period}"] = result["close"].rolling(window=period, min_periods=1).mean()
    return result


def calc_macd(df, fast=12, slow=26, signal=9):
    result = df.copy()
    ema_fast = result["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = result["close"].ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    macd_signal = macd.ewm(span=signal, adjust=False).mean()
    result["macd"] = macd
    result["macd_signal"] = macd_signal
    result["macd_hist"] = macd - macd_signal
    return result


def calc_rsi(df, period=14):
    result = df.copy()
    delta = result["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    result["rsi"] = rsi.fillna(50)
    return result


def calc_kd(df, period=9, k_smooth=3, d_smooth=3):
    result = df.copy()
    low_min = result["low"].rolling(window=period, min_periods=1).min()
    high_max = result["high"].rolling(window=period, min_periods=1).max()
    rsv = (result["close"] - low_min) / (high_max - low_min).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / k_smooth, adjust=False).mean()
    d = k.ewm(alpha=1 / d_smooth, adjust=False).mean()
    result["k"] = k
    result["d"] = d
    return result


def calc_bias(df, period=20):
    result = df.copy()
    ma = result["close"].rolling(window=period, min_periods=1).mean()
    result["bias"] = (result["close"] - ma) / ma.replace(0, np.nan) * 100
    return result


def calc_bollinger(df, period=20, num_std=2):
    result = df.copy()
    mid = result["close"].rolling(window=period, min_periods=1).mean()
    std = result["close"].rolling(window=period, min_periods=1).std().fillna(0)
    result["boll_mid"] = mid
    result["boll_upper"] = mid + num_std * std
    result["boll_lower"] = mid - num_std * std
    return result


def calc_atr(df, period=14):
    result = df.copy()
    prev_close = result["close"].shift(1)
    tr = pd.concat([
        result["high"] - result["low"],
        (result["high"] - prev_close).abs(),
        (result["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    result["atr"] = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return result


def calc_dmi_adx(df, period=14):
    result = df.copy()
    high = result["high"]
    low = result["low"]
    close = result["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_dm_smooth = pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    minus_dm_smooth = pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    plus_di = 100 * plus_dm_smooth / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm_smooth / atr.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    result["plus_di"] = plus_di.fillna(0)
    result["minus_di"] = minus_di.fillna(0)
    result["adx"] = adx.fillna(0)
    return result


# ============================================================
# 均線/KD 交叉、突破支撐壓力
# ============================================================
def calc_ma_cross(df, short=5, long=20):
    result = df.copy()
    ma_short = result["close"].rolling(window=short, min_periods=1).mean()
    ma_long = result["close"].rolling(window=long, min_periods=1).mean()
    prev_short = ma_short.shift(1)
    prev_long = ma_long.shift(1)
    result["ma_golden_cross"] = ((prev_short <= prev_long) & (ma_short > ma_long)).fillna(False)
    result["ma_death_cross"] = ((prev_short >= prev_long) & (ma_short < ma_long)).fillna(False)
    return result


def calc_kd_cross(df):
    n = len(df)
    golden_weight = np.zeros(n)
    death_weight = np.zeros(n)

    if "k" not in df.columns or "d" not in df.columns:
        return pd.DataFrame({"kd_golden_weight": golden_weight, "kd_death_weight": death_weight}, index=df.index)

    k = df["k"].values
    d = df["d"].values
    vol = df["volume"].values if "volume" in df.columns else np.zeros(n)
    vol_ma20 = pd.Series(vol).rolling(window=20, min_periods=1).mean().values

    for i in range(1, n):
        golden = k[i - 1] <= d[i - 1] and k[i] > d[i]
        death = k[i - 1] >= d[i - 1] and k[i] < d[i]
        volume_confirmed = vol_ma20[i] > 0 and vol[i] >= VOLUME_CONFIRM_MULTIPLIER * vol_ma20[i]
        weight = 1.0 if volume_confirmed else 0.5
        if golden:
            golden_weight[i] = weight
        if death:
            death_weight[i] = weight

    return pd.DataFrame({"kd_golden_weight": golden_weight, "kd_death_weight": death_weight}, index=df.index)


def calc_breakout(df, window=20):
    n = len(df)
    up_weight = np.zeros(n)
    down_weight = np.zeros(n)

    close = df["close"].values
    high = df["high"].values if "high" in df.columns else close
    low = df["low"].values if "low" in df.columns else close
    vol = df["volume"].values if "volume" in df.columns else np.zeros(n)
    vol_ma20 = pd.Series(vol).rolling(window=20, min_periods=1).mean().values

    resistance = pd.Series(high).rolling(window=window, min_periods=1).max().shift(1).values
    support = pd.Series(low).rolling(window=window, min_periods=1).min().shift(1).values

    for i in range(1, n):
        if np.isnan(resistance[i]) or np.isnan(support[i]):
            continue
        volume_confirmed = vol_ma20[i] > 0 and vol[i] >= VOLUME_CONFIRM_MULTIPLIER * vol_ma20[i]
        weight = 1.0 if volume_confirmed else 0.5
        if close[i] > resistance[i] and close[i - 1] <= resistance[i]:
            up_weight[i] = weight
        if close[i] < support[i] and close[i - 1] >= support[i]:
            down_weight[i] = weight

    return pd.DataFrame({"breakout_up_weight": up_weight, "breakout_down_weight": down_weight}, index=df.index)


# ============================================================
# K棒型態偵測
# ============================================================
def detect_hammer_hanging_man(df, body_ratio=0.3, shadow_ratio=2.0):
    result = df.copy()
    o, h, l, c = result["open"], result["high"], result["low"], result["close"]
    body = (c - o).abs()
    range_ = (h - l).replace(0, np.nan)
    upper_shadow = h - np.maximum(o, c)
    lower_shadow = np.minimum(o, c) - l

    small_body = body <= range_ * body_ratio
    long_lower_shadow = lower_shadow >= body * shadow_ratio
    short_upper_shadow = upper_shadow <= body * 0.5

    is_candidate = small_body & long_lower_shadow & short_upper_shadow

    prev_trend_down = c.shift(1) < c.shift(6)
    prev_trend_up = c.shift(1) > c.shift(6)

    result["hammer"] = (is_candidate & prev_trend_down).fillna(False)
    result["hanging_man"] = (is_candidate & prev_trend_up).fillna(False)
    return result


def detect_engulfing(df):
    result = df.copy()
    o, c = result["open"], result["close"]
    prev_o, prev_c = o.shift(1), c.shift(1)

    bullish = (prev_c < prev_o) & (c > o) & (c >= prev_o) & (o <= prev_c)
    bearish = (prev_c > prev_o) & (c < o) & (c <= prev_o) & (o >= prev_c)

    result["bullish_engulfing"] = bullish.fillna(False)
    result["bearish_engulfing"] = bearish.fillna(False)
    return result


def detect_morning_evening_star(df):
    result = df.copy()
    o, c = result["open"], result["close"]
    body = (c - o).abs()

    o1, c1, body1 = o.shift(2), c.shift(2), body.shift(2)
    o2, body2 = o.shift(1), body.shift(1)
    o3, c3 = o, c

    day1_bear = c1 < o1
    day1_bull = c1 > o1
    day2_small = body2 <= body1 * 0.5
    day3_bull = c3 > o3
    day3_bear = c3 < o3
    mid1 = (o1 + c1) / 2
    day3_covers_half_up = c3 >= mid1
    day3_covers_half_down = c3 <= mid1

    morning = day1_bear & day2_small & day3_bull & day3_covers_half_up
    evening = day1_bull & day2_small & day3_bear & day3_covers_half_down

    result["morning_star"] = morning.fillna(False)
    result["evening_star"] = evening.fillna(False)
    return result


def _local_extrema(prices, window=5):
    """找出簡化的區域高低點索引"""
    n = len(prices)
    peaks, troughs = [], []
    for i in range(window, n - window):
        segment = prices[i - window:i + window + 1]
        if prices[i] == max(segment):
            peaks.append(i)
        if prices[i] == min(segment):
            troughs.append(i)
    return peaks, troughs


def detect_double_top_bottom(df, window=5, tolerance=0.02, min_gap=5):
    result = df.copy()
    n = len(result)
    double_top = np.zeros(n, dtype=bool)
    double_bottom = np.zeros(n, dtype=bool)

    price = result["close"].values
    peaks, troughs = _local_extrema(price, window=window)

    for j in range(1, len(peaks)):
        curr_i, prev_i = peaks[j], peaks[j - 1]
        if curr_i - prev_i < min_gap:
            continue
        if price[prev_i] <= 0:
            continue
        if abs(price[curr_i] - price[prev_i]) / price[prev_i] <= tolerance:
            double_top[curr_i] = True

    for j in range(1, len(troughs)):
        curr_i, prev_i = troughs[j], troughs[j - 1]
        if curr_i - prev_i < min_gap:
            continue
        if price[prev_i] <= 0:
            continue
        if abs(price[curr_i] - price[prev_i]) / price[prev_i] <= tolerance:
            double_bottom[curr_i] = True

    result["double_top"] = double_top
    result["double_bottom"] = double_bottom
    return result


# ============================================================
# 背離偵測
# ============================================================
def detect_divergence(df, window=5, lookback=60):
    result = df.copy()
    n = len(result)
    divergence = np.array([None] * n, dtype=object)

    if "rsi" not in result.columns:
        result["divergence_rsi"] = divergence
        return result

    price = result["close"].values
    rsi = result["rsi"].values
    peaks, troughs = _local_extrema(price, window=window)

    for j in range(1, len(peaks)):
        curr_i, prev_i = peaks[j], peaks[j - 1]
        if curr_i - prev_i > lookback:
            continue
        if price[curr_i] > price[prev_i] and rsi[curr_i] < rsi[prev_i]:
            divergence[curr_i] = "bearish"

    for j in range(1, len(troughs)):
        curr_i, prev_i = troughs[j], troughs[j - 1]
        if curr_i - prev_i > lookback:
            continue
        if price[curr_i] < price[prev_i] and rsi[curr_i] > rsi[prev_i]:
            divergence[curr_i] = "bullish"

    result["divergence_rsi"] = divergence
    return result


# ============================================================
# 洗盤（訊號反覆）偵測
# ============================================================
def detect_whipsaw(df, lookback=5, flip_count_threshold=2):
    result = df.copy()
    n = len(result)
    whipsaw = np.zeros(n, dtype=bool)

    if "macd_hist" not in result.columns:
        result["whipsaw"] = whipsaw
        return result

    sign = np.sign(result["macd_hist"].fillna(0)).values

    for i in range(lookback, n):
        window = sign[i - lookback:i + 1]
        flips = np.sum(np.diff(window) != 0)
        if flips >= flip_count_threshold:
            whipsaw[i] = True

    result["whipsaw"] = whipsaw
    return result


# ============================================================
# 綜合評分與買賣決策
# ============================================================
def generate_signals(df, buy_threshold=5, sell_threshold=-5):
    """
    根據多項技術指標計算綜合評分與買賣決策。
    門檻：買進 >= buy_threshold，賣出 <= sell_threshold，其餘為觀望。
    """
    result = df.copy()

    result = calc_ma(result)
    result = calc_macd(result)
    result = calc_rsi(result)
    result = calc_kd(result)
    result = calc_bias(result)
    result = calc_bollinger(result)
    result = calc_atr(result)
    result = calc_dmi_adx(result)
    result = calc_ma_cross(result)
    kd_cross_df = calc_kd_cross(result)
    breakout_df = calc_breakout(result)
    result = detect_hammer_hanging_man(result)
    result = detect_engulfing(result)
    result = detect_morning_evening_star(result)
    result = detect_double_top_bottom(result)
    result = detect_divergence(result)
    result = detect_whipsaw(result)

    score = pd.Series(0.0, index=df.index)

    # --- 基礎指標 ---
    score += np.sign(result["macd_hist"]).fillna(0)
    score += np.where(result["rsi"] < 30, 0.5, np.where(result["rsi"] > 70, -0.5, 0))
    score += np.where(result["k"] > result["d"], 1, -1)
    score += np.where(df["close"] > result["ma60"], 1, -1)
    score += np.where(result["bias"] < -5, 1, np.where(result["bias"] > 5, -1, 0))
    score += np.where(df["close"] <= result["boll_lower"], 0.5,
                       np.where(df["close"] >= result["boll_upper"], -0.5, 0))

    trend_dir = np.where(result["plus_di"] > result["minus_di"], 1, -1)
    score += np.where(result["adx"] > 20, trend_dir, 0)

    # --- 均線/KD交叉、突破支撐壓力 ---
    score += np.where(result["ma_golden_cross"], 1, np.where(result["ma_death_cross"], -1, 0))
    score += kd_cross_df["kd_golden_weight"] - kd_cross_df["kd_death_weight"]
    score += breakout_df["breakout_up_weight"] - breakout_df["breakout_down_weight"]

    # --- K棒型態 ---
    score += np.where(result["hammer"], 0.5, np.where(result["hanging_man"], -0.5, 0))
    score += np.where(result["bullish_engulfing"], 1, np.where(result["bearish_engulfing"], -1, 0))
    score += np.where(result["morning_star"], 1, np.where(result["evening_star"], -1, 0))
    score += np.where(result["double_bottom"], PATTERN_WEIGHT_DOUBLE_TOP_BOTTOM,
                       np.where(result["double_top"], -PATTERN_WEIGHT_DOUBLE_TOP_BOTTOM, 0))

    # --- 背離加分/扣分 ---
    score += np.where(result["divergence_rsi"] == "bullish", 1,
                       np.where(result["divergence_rsi"] == "bearish", -1, 0))

    # --- 洗盤警示 ---
    score = np.where(result["whipsaw"], score * 0.5, score)

    result["score"] = score
    result["decision"] = np.select(
        [result["score"] >= buy_threshold, result["score"] <= sell_threshold],
        ["買進", "賣出"], default="觀望",
    )

    return result


# ============================================================
# 進出場價位計算
# ============================================================
def calculate_price_levels(df, result, atr_multiplier=1.5, rr_ratio=3.0, sr_window=20):
    levels = pd.DataFrame(index=result.index)
    close = df["close"]
    atr = result["atr"] if "atr" in result.columns else pd.Series(np.nan, index=result.index)
    support = df["low"].rolling(window=sr_window, min_periods=1).min()
    resistance = df["high"].rolling(window=sr_window, min_periods=1).max()

    entry_price = close.copy()
    stop_loss_atr = np.where(
        result["decision"] == "買進", entry_price - atr_multiplier * atr,
        np.where(result["decision"] == "賣出", entry_price + atr_multiplier * atr, np.nan)
    )
    stop_loss_support = np.where(
        result["decision"] == "買進", support,
        np.where(result["decision"] == "賣出", resistance, np.nan)
    )

    risk = np.abs(entry_price - stop_loss_atr)
    take_profit_rr = np.where(
        result["decision"] == "買進", entry_price + rr_ratio * risk,
        np.where(result["decision"] == "賣出", entry_price - rr_ratio * risk, np.nan)
    )
    take_profit_resistance = np.where(
        result["decision"] == "買進", resistance,
        np.where(result["decision"] == "賣出", support, np.nan)
    )

    levels["entry_price"] = np.where(result["decision"] != "觀望", entry_price, np.nan)
    levels["stop_loss_atr"] = stop_loss_atr
    levels["stop_loss_support"] = stop_loss_support
    levels["take_profit_rr"] = take_profit_rr
    levels["take_profit_resistance"] = take_profit_resistance

    return levels


# ============================================================
# 歷史訊號回測統計（非預測，僅供參考）
# ============================================================
def backtest_signal_returns(df, result, horizons=(63, 126), min_sample_warn=10):
    close = df["close"].reset_index(drop=True)
    decision = result["decision"].reset_index(drop=True)
    n = len(close)

    stats = {}
    for horizon in horizons:
        stats[horizon] = {}
        for target_decision in ("買進", "賣出"):
            idxs = decision[decision == target_decision].index.tolist()
            returns = []
            details = []
            for i in idxs:
                future_i = i + horizon
                if future_i >= n:
                    continue
                base_price = close.iloc[i]
                if base_price <= 0:
                    continue
                future_price = close.iloc[future_i]
                ret_pct = (future_price - base_price) / base_price * 100
                returns.append(ret_pct)
                details.append({"signal_index": int(i), "return_pct": round(ret_pct, 2)})

            entry = {
                "count": len(returns),
                "mean_return_pct": round(float(np.mean(returns)), 2) if returns else None,
                "median_return_pct": round(float(np.median(returns)), 2) if returns else None,
                "win_rate_pct": round(float(np.mean([1 if r > 0 else 0 for r in returns]) * 100), 1) if returns else None,
                "min_return_pct": round(float(np.min(returns)), 2) if returns else None,
                "max_return_pct": round(float(np.max(returns)), 2) if returns else None,
                "details": details,
            }
            if len(returns) < min_sample_warn:
                entry["note"] = f"樣本數僅 {len(returns)} 筆，統計參考性有限"

            stats[horizon][target_decision] = entry

    return stats
