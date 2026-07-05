# 台股每日監控機器人（免費架構）

## 架構
GitHub Actions（排程）→ 證交所 OpenAPI（免費資料源）→ pandas 運算與篩選
→ LINE Messaging API / Telegram Bot 推播 → CSV 存回 repo 累積歷史

## 使用步驟

### 1. 建立 GitHub repo
把 `monitor.py`、`.github/workflows/daily_monitor.yml` 放進你的 repo。

### 2. 申請 LINE Messaging API（免費，每月200則）
1. 到 [LINE Developers](https://developers.line.biz/) 建立 Provider 與 Channel（Messaging API 類型）
2. 取得 `Channel Access Token`
3. 取得推播對象 ID：
   - 個人：加官方帳號好友後，用 Webhook 或 LINE Developers 的測試工具取得 `userId`
   - 群組：把官方帳號拉進群組（需先在官方帳號設定開啟「允許被邀請加入群組」），透過 Webhook 事件取得 `groupId`

### 3. 申請 Telegram Bot（免費，無則數上限，作為備援）
1. 在 Telegram 搜尋 `@BotFather`，用 `/newbot` 建立機器人，取得 `Bot Token`
2. 把機器人加入你的聊天或群組，用 `https://api.telegram.org/bot<TOKEN>/getUpdates` 取得 `chat_id`

### 4. 在 GitHub repo 設定 Secrets
到 repo 的 `Settings > Secrets and variables > Actions`，新增：
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_TARGET_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 5. 測試
到 GitHub repo 的 Actions 頁籤，手動觸發 `台股每日監控` workflow（workflow_dispatch），
確認 LINE / Telegram 有收到訊息，且 `docs/data/` 資料夾有新的 CSV 檔案。

### 6. 開啟 GitHub Pages 儀表板（手機也能看的網頁）

1. 確認 repo 裡有 `docs/index.html`（本次新增的儀表板網頁）
2. 到 repo 的 `Settings > Pages`
3. **Source** 選擇 `Deploy from a branch`，**Branch** 選 `main`，資料夾選 `/docs`，儲存
4. 等 1-2 分鐘後，會出現一個網址，例如 `https://你的帳號.github.io/repo名稱/`
5. 用手機瀏覽器打開這個網址，可以「加入主畫面」，體驗上就像一個 App 圖示

儀表板會自動讀取 `docs/data/latest.csv`（由 `monitor.py` 每次執行時產生），
畫面包含：頂部跑馬燈、核心自選股卡片、動態觀察清單分類清單。
如果還沒有真實資料，畫面會先顯示示範資料並提示尚未偵測到檔案，這是正常現象。

## 重大更新：資料路徑修正 + K線圖 + 關注股票機制

**⚠️ 重要修正**：之前版本 `monitor.py` 把資料存到 `data/` 資料夾，但 GitHub Pages
只公開發布 `docs/` 資料夾，導致網頁其實抓不到真實資料。這次已修正，所有資料改存到
**`docs/data/`** 底下。如果你的 repo 裡還有舊的 `data/` 資料夾，可以直接刪除，不影響運作。

### 新增功能

1. **歷史資料保留半年**：`docs/data/history.json` 現在保留約130個交易日（近半年）的
   OHLC（開高低收）資料，並自動計算 MA5、MA20、KD 指標
2. **K線圖查詢**：網頁新增「個股歷史走勢」區塊，用免費開源套件 lightweight-charts
   畫出蠟燭圖，疊加 MA5/MA20 均線，並顯示最新 K/D 數值
3. **關注股票管理（免費的網頁互動機制）**：
   - 網頁上輸入股票代號、按「送出關注請求」，會開一個**預先填好內容的 GitHub Issue**頁面
   - 你按下 GitHub 的「Submit new issue」送出後，下一次排程執行時，
     `monitor.py` 會自動讀取這個 Issue、把股票加入監控清單、開始推播提醒，並自動關閉該 Issue
   - 這個機制完全免費，不需要架設額外的伺服器，也不會讓任何金鑰暴露在網頁上

### 這個機制的限制（誠實說明）

- 不是「按下去馬上推播」，而是「送出請求 → 排程下次執行時生效」，最長要等到下一個交易日下午2:15
- K線圖只有「核心自選股」和「已核准的關注股票」才有歷史資料，動態觀察清單（價格異常等）
  因為每天篩選出來的股票都不同，沒有持續累積歷史，所以不會出現在K線圖選單裡

## 這次更新的網頁功能（淺色主題版）

- **配色改為淺色系**：頁面底色改成暖白色，卡片為白底，頂部跑馬燈維持深色招牌條做對比
- **卡片走勢小圖表**：核心自選股卡片下方會顯示近期收盤價走勢的小圖表（sparkline），
  資料來源是 `docs/data/history.json`，由 `monitor.py` 每次執行時自動累積（目前保留約半年歷史）
  ***剛上線的前幾天會顯示「尚無多日歷史資料」，這是正常現象，累積幾天後就會出現線圖***
- **今日新聞區塊**：串接證交所 OpenAPI 的「重大訊息」端點（`t187ap04_L`，就是公開資訊觀測站的資料來源），
  免費、不需要金鑰，顯示在網頁最下方，資料存在 `docs/data/news.csv`
- **版面縮窄至85%寬度**：桌機瀏覽器下內容區域維持在螢幕的85%寬度置中顯示，文字大小不受影響；
  手機瀏覽器因螢幕本身較窄，改為92%寬度，避免內容太擠
- **跑馬燈速度放慢**：捲動一輪時間從40秒延長到90秒
- **手機文字放大**：手機瀏覽器下的股價、跑馬燈、新聞標題字級都比之前放大

## 下一步可以擴充的部分
- **技術指標升級**：目前已有 MA5、MA20、KD，如果想加 MACD、布林通道等更多指標，
  邏輯一樣寫在 `monitor.py` 的技術指標區塊，再讓網頁的K線圖多疊加一條線即可
- **公開資訊觀測站公告**：可另外串接 MOPS 的公告清單，比對關鍵字後納入動態觀察清單
- **法人買賣超**：目前抓的是 `T86` 端點示範用，正式使用前建議先實際呼叫確認欄位名稱
  （證交所 OpenAPI 部分端點欄位名稱會調整，建議上線前用 Swagger UI 核對）
- **推播分級**：目前是核心股+動態清單合併成一則，如果訊息長度過長，
  可以依優先度拆成「立即推播」與「僅存入 CSV，週報彙整」兩種
