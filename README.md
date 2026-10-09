# LLM Usage Bot

一個 Discord bot，用 `/usage` 一次查看 Claude 與 Codex 的訂閱額度、重置時間與 API 花費。
超過 `AUTO_REPORT_MINUTES` 分鐘沒有手動查詢時，會自動發一次用量；5 小時與每週額度重置前 `REMINDER_MINUTES` 分鐘（預設 60）會先提醒並附上目前用量，重置時也會發通知。
通知依 `NOTIFY_MODE` 發到 `NOTIFY_CHANNEL_ID` 頻道（`channel`）、私訊給 `DISCORD_ALLOWED_USER_IDS` 的人（`dm`），或兩者（`both`）。私訊需要 bot 和你在同一個伺服器。
程式在本機執行，只對外連到 Discord，不開任何連接埠。

## 資料來源

| | 訂閱額度 | API 花費（選用） |
|---|---|---|
| Claude | `claude -p /usage`（Claude Code 的本地指令），每 `CLAUDE_POLL_MINUTES` 分鐘查一次，即時、不消耗額度，終端機／VS Code／網頁的用量都算在內。失敗時改用狀態列寫入的快取 | Anthropic Admin API `cost_report`（`ANTHROPIC_ADMIN_KEY`） |
| Codex | `codex app-server` 的 `account/rateLimits/read`，每次查詢即時、不消耗額度 | OpenAI `organization/costs`（`OPENAI_ADMIN_KEY`） |

兩邊都不讀取 Claude／Codex 的登入憑證。

## 安裝

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # 再填入內容
```

Claude：bot 會直接執行 `claude -p /usage`，只要 `claude` 在 PATH 裡、並以 Pro/Max 訂閱登入即可，不需要其他設定。

選用的備援：狀態列快取。`claude /usage` 失敗時（例如 bot 執行環境找不到 `claude`），會改讀這份快取，embed 會標示「來源：狀態列快取」。在 `~/.claude/settings.json` 加入以下設定，`<專案路徑>` 換成這個專案的絕對路徑（用 `/`，例如 `E:/workspace/usage-checker`）。Claude Code 會在任意目錄執行這行指令，所以必須是絕對路徑；腳本只用標準函式庫，不需要 venv。

```json
"statusLine": {
  "type": "command",
  "command": "py -3 \"<專案路徑>/statusline/claude_statusline.py\""
}
```

設定好之後，在**終端機**執行 `claude` 並送出任意一句 prompt（例如 `hi`），等模型回應完成。額度資料是模型回應時才附帶的，所以：

- 只開 `claude` 不發問，或只用 `/help`、`/usage` 這類本地 slash command，都拿不到資料。
- VS Code 擴充套件不會執行狀態列，必須用終端機的 `claude`。
- 只有 Pro/Max 訂閱登入才有額度資料，API key 登入沒有。

成功後終端機底部會顯示 `5h 8% (4h28m) | 7d 25% (3d)` 這類文字，快取寫在 `%LOCALAPPDATA%\llm-usage-bot\claude_rate_limits.json`。

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

## 版本管理

使用 [commitizen](https://commitizen-tools.github.io/commitizen/)，commit 訊息遵循 Conventional Commits（`feat:`、`fix:`、`refactor:`…）。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\cz commit   # 互動式寫 commit 訊息
.\.venv\Scripts\cz bump     # 依 commit 決定新版本、更新 CHANGELOG.md、打 tag
git push --follow-tags
```
