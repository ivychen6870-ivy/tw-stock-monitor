"""
更新片段：generate_signals() + PATTERN_WEIGHT_DOUBLE_TOP_BOTTOM

用途：貼到您 GitHub repo 的 tw_stock_indicators.py，取代原本的
generate_signals() 函式與 PATTERN_WEIGHT_DOUBLE_TOP_BOTTOM 常數。

本次調整內容：
1. 買賣門檻：8 -> 5（buy_threshold / sell_threshold）
2. 降低較容易誤判的指標權重：
   - RSI 超買超賣：±1 -> ±0.5
   - 布林通道觸及上下軌：±1 -> ±0.5
   - 錘子線/吊人線：±1 -> ±0.5
   - 雙重頂/雙重底：0.5 -> 0.3

注意：這不是完整的 tw_stock_indicators.py 檔案，只包含需要修改的
函式與常數。因為上一輪對話中斷、Claude 端工作環境被清空，目前沒有
您原始檔案裡其他函式（calc_ma、calc_macd、calc_rsi...等）的實際內容，
無法保證逐字重建完全一致，所以這裡只提供「確定需要改的部分」，
避免覆蓋掉您檔案裡其他沒討論到的細節。
"""

# ============================================================
# 常數：放在檔案上方，跟其他常數定義放一起
# ============================================================
PATTERN_WEIGHT_DOUBLE_TOP_BOTTOM = 0.3   # 原本是 0.5


# ============================================================
# 函式：取代原本的 generate_signals()
# ============================================================
def generate_signals(df, buy_threshold=5, sell_threshold=-5):
    """
    根據多項技術指標計算綜合評分與買賣決策。
    門檻：買進 >= buy_threshold，賣出 <= sell_threshold，其餘為觀望。
    """
    result = df.copy()

    # 計算各項指標（沿用既有的計算函式，名稱需與您檔案內一致）
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
    # MACD 柱狀圖方向（可靠度高，維持 ±1）
    score += np.sign(result["macd_hist"]).fillna(0)

    # RSI 超買超賣（雜訊較高，權重降為 ±0.5）
    score += np.where(result["rsi"] < 30, 0.5, np.where(result["rsi"] > 70, -0.5, 0))

    # KD 位置（維持 ±1）
    score += np.where(result["k"] > result["d"], 1, -1)

    # MA60 多空排列（可靠度高，維持 ±1）
    score += np.where(df["close"] > result["ma60"], 1, -1)

    # BIAS 乖離率（維持 ±1）
    score += np.where(result["bias"] < -5, 1, np.where(result["bias"] > 5, -1, 0))

    # 布林通道觸及上下軌（雜訊較高，權重降為 ±0.5）
    score += np.where(df["close"] <= result["boll_lower"], 0.5,
                       np.where(df["close"] >= result["boll_upper"], -0.5, 0))

    # DMI/ADX 趨勢確認（可靠度高，需 ADX>20 才計分，維持 ±1）
    trend_dir = np.where(result["plus_di"] > result["minus_di"], 1, -1)
    score += np.where(result["adx"] > 20, trend_dir, 0)

    # --- 均線/KD交叉、突破支撐壓力（已含量能確認，維持原權重） ---
    score += np.where(result["ma_golden_cross"], 1, np.where(result["ma_death_cross"], -1, 0))
    score += kd_cross_df["kd_golden_weight"] - kd_cross_df["kd_death_weight"]
    score += breakout_df["breakout_up_weight"] - breakout_df["breakout_down_weight"]

    # --- K棒型態 ---
    # 錘子線/吊人線（單根K棒可靠度較低，權重降為 ±0.5）
    score += np.where(result["hammer"], 0.5, np.where(result["hanging_man"], -0.5, 0))

    # 多頭/空頭吞噬（型態明確，維持 ±1）
    score += np.where(result["bullish_engulfing"], 1, np.where(result["bearish_engulfing"], -1, 0))

    # 晨星/暮星（型態明確，維持 ±1）
    score += np.where(result["morning_star"], 1, np.where(result["evening_star"], -1, 0))

    # 雙重頂/雙重底（判斷本身較粗略，權重由 0.5 再降為 0.3）
    score += np.where(result["double_bottom"], PATTERN_WEIGHT_DOUBLE_TOP_BOTTOM,
                       np.where(result["double_top"], -PATTERN_WEIGHT_DOUBLE_TOP_BOTTOM, 0))

    # --- 背離加分/扣分（趨勢反轉訊號可靠度較高，維持 ±1） ---
    score += np.where(result["divergence_rsi"] == "bullish", 1,
                       np.where(result["divergence_rsi"] == "bearish", -1, 0))

    # --- 洗盤警示：近期訊號反覆時，分數打折 ---
    score = np.where(result["whipsaw"], score * 0.5, score)

    result["score"] = score
    result["decision"] = np.select(
        [result["score"] >= buy_threshold, result["score"] <= sell_threshold],
        ["買進", "賣出"], default="觀望",
    )

    return result
