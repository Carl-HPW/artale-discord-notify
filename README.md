# Artale Discord Notify

每 10 分鐘檢查 [Artale 官方公告](https://artale.live/tw/news)。發現新公告時，透過 Discord Webhook 傳送至指定頻道。

## 啟用方式

1. 在 Discord 目標頻道建立 Webhook 並複製網址。
2. 進入本 Repository 的 `Settings` → `Secrets and variables` → `Actions`。
3. 新增 Repository secret：
   - Name：`DISCORD_WEBHOOK_URL`
   - Secret：Discord Webhook 完整網址
4. 進入 `Actions` → `Artale News Monitor` → `Run workflow`，手動測試一次。

排程使用 UTC，每小時的第 3、13、23、33、43、53 分執行。GitHub Actions 排程可能因平台負載而延遲數分鐘。

## 防止重複通知

`seen.json` 保存已處理的公告 ID。首次初始化只會記錄當時既有公告，不會一次推送所有舊公告；通知成功後才會把新公告記為已處理。

## 手動執行

```bash
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." python3 monitor.py
```

程式只使用 Python 標準函式庫，不需安裝額外套件。
