# LLM Usage Bot

一個 Discord bot，用 `/usage` 一次查看 Claude 與 Codex 的訂閱額度、重置時間與 API 花費。
超過 `AUTO_REPORT_MINUTES` 分鐘沒有手動查詢時，會自動把用量發到 `NOTIFY_CHANNEL_ID` 頻道；額度重置時也會發通知。
程式在本機執行，只對外連到 Discord，不開任何連接埠。

## 資料來源

| | 訂閱額度 | API 花費（選用） |
|---|---|---|
| Claude | Claude Code 狀態列提供的 `rate_limits`，由 `statusline/claude_statusline.py` 寫入快取。只在使用 Claude Code 時更新 | Anthropic Admin API `cost_report`（`ANTHROPIC_ADMIN_KEY`） |
| Codex | `codex app-server` 的 `account/rateLimits/read`，每次查詢即時、不消耗額度 | OpenAI `organization/costs`（`OPENAI_ADMIN_KEY`） |

兩邊都不讀取 Claude／Codex 的登入憑證。

## 安裝

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # 再填入內容
```

Claude 狀態列（`~/.claude/settings.json`）：

```json
"statusLine": {
  "type": "command",
  "command": "\"C:/Users/kzsu/workspace/llm-usage-checker-and-notification/.venv/Scripts/python.exe\" \"C:/Users/kzsu/workspace/llm-usage-checker-and-notification/statusline/claude_statusline.py\""
}
```

## Discord 設定

1. [Developer Portal](https://discord.com/developers/applications) 建一個應用程式，在 **Bot** 頁面按 **Reset Token** 取得 token，填入 `.env` 的 `DISCORD_BOT_TOKEN`。不需要開任何 Privileged Intent。
2. **OAuth2 → URL Generator** 勾選 `bot`、`applications.commands`，權限勾 `View Channels`、`Send Messages`，用產生的網址把 bot 邀請進你的私人伺服器。
3. Discord 設定 → 進階 → 開啟「開發者模式」，之後在伺服器、頻道、自己的頭像上按右鍵 →「複製 ID」，填入 `DISCORD_GUILD_ID`、`NOTIFY_CHANNEL_ID`、`DISCORD_ALLOWED_USER_IDS`。

## 執行

```powershell
.\.venv\Scripts\python.exe -m llm_usage_bot
```

或開視窗版（看得到連線狀態與執行輸出，關掉視窗就斷線）：

```powershell
.\.venv\Scripts\pythonw.exe -m llm_usage_bot.gui
```

視窗版每 2 秒顯示 Discord gateway 連線狀態與延遲，每 60 秒用 Discord REST API 做一次健康檢查（也可按「立即健康檢查」）。

定時回報與重置通知需要程式持續執行。
