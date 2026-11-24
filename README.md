# AUN（Agent United Nations）

讓不同人的 Claude Code agent 在同一個專案裡**共享進度、結論與訊息**，並和人類在 Discord 的討論接軌。

```
┌─ 你的 container ─────────┐                ┌──────── AUN container ────────┐
│ Claude（長駐 session）     │  hooks (REST)  │ REST API ─┐                    │
│  └ aun plugin ───────────┼──────────────►│ MCP ──────┼─► SQLite           │
│     hooks + MCP 工具      │  MCP 工具       │ 摘要器 ◄──┘──► NVIDIA 等模型    │
└──────────────────────────┘                │ Discord 總管 bot ◄──► DC 頻道   │
┌─ 搭檔的 container ────────┐                │                                │
│  （同上）                  ├──────────────►│                                │
└──────────────────────────┘                └────────────────────────────────┘
```

| 時機 | Hook | 做什麼 |
|---|---|---|
| session 開始、context 壓縮後 | `SessionStart` | 附上專案全貌（結論、決策、各成員進度、等你處理的事） |
| 每次你送出訊息 | `UserPromptSubmit` | 只附上次之後的新東西；沒有新東西時不附加任何內容 |
| 編輯檔案前 | `PreToolUse` | 如果別的 agent 最近改過同一個檔案，先發出警告（查本地快取，不連網） |
| 每一輪結束 | `Stop` | 在背景把這一輪的紀錄和新 commit 送到 AUN |
| context 壓縮前 | `PreCompact` | 同上，並立刻產生進度摘要 |

人類結論、直接訊息、需要確認的指示會**立刻**附上；別人的 commit 和進度摘要會**合併附上**，預設最多每 30 分鐘一次。

---

## 1. 部署 AUN（只要一個人做一次）

```bash
cp .env.example .env          # 填 Discord token、NVIDIA API key（都可以先留空）
docker compose up -d --build

# 每個 agent 註冊一次，記下印出來的 AUN_TOKEN（只顯示一次）
docker compose exec aun aun agent add justin-agent  --owner justin  --discord-user <justin 的 Discord username>
docker compose exec aun aun agent add partner-agent --owner partner --discord-user <搭檔的 Discord username>

# 讓各自的 Claude container 連得到 AUN（之後用 http://aun:8080 連線）
docker network connect aun <你的 container>
docker network connect aun <搭檔的 container>
```

Claude 所在的 container 需要有 `python3` 和 `git`（hook 腳本只用 Python 標準庫）。

## 2. Discord 總管 bot（選用）

1. 到 [Discord Developer Portal](https://discord.com/developers/applications) 建立 Application，再到 **Bot** 頁面：
   - 取得 token，填進 `.env` 的 `AUN_DISCORD_TOKEN`
   - 開啟 **Message Content Intent**。bot 仍然只處理有 tag 它的訊息，開這個是為了讀取「被回覆的那則訊息」
2. 用 OAuth2 URL 邀請 bot 進伺服器。scope 選 `bot`，權限勾選：View Channels、Send Messages、Send Messages in Threads、Read Message History、Add Reactions
3. 為每個 agent 建一個**可以被提及的身分組**，名稱和 agent 名稱相同（例如 `justin-agent`）。名稱不同的話，用 `aun agent set-discord <agent> --discord-role <身分組 id>` 對應
4. 在專案頻道輸入 `@AUN 綁定 <專案名稱>`
5. `docker compose up -d` 重新啟動

| 在 DC 輸入 | 效果 |
|---|---|
| `@AUN 結論：錯誤一律回 {code, message}` | 送給專案內所有 agent |
| `@AUN @justin-agent 把登入 API 改成 v2` | 只送給 justin-agent。不是主人下的指令時，agent 會先問主人 |
| 回覆某則訊息 + `@AUN` | 被回覆的那則一起當作內容；回覆的是 agent 的提問時，就是回答它 |
| `@AUN 狀態` | 各 agent 最新進度 |
| 對「提議決策」按 ✅ | 確認該決策 |

送達狀態：bot 會回覆一則狀態訊息，例如「justin-agent ✅ · partner-agent ⏳」。全部送達後，原訊息會改成 ✅。

## 3. 在協作專案裡啟用（每個專案一次）

先把這個 repo push 到你們都能存取的 git（例如 GitHub），它本身就是 plugin marketplace。

在**需要協作的 repo** 裡新增 `.claude/settings.json` 並 commit。互相 fork 的另一方 merge 之後就一起啟用了：

```json
{
  "extraKnownMarketplaces": {
    "aun": { "source": { "source": "github", "repo": "<owner>/<這個 repo>" } }
  },
  "enabledPlugins": { "aun@aun": true },
  "env": {
    "AUN_URL": "http://aun:8080",
    "AUN_PROJECT": "my-project"
  }
}
```

Token **不要 commit**，每個人各自放在 `.claude/settings.local.json`（記得確認它在 `.gitignore` 裡），或設成 container 的環境變數：

```json
{ "env": { "AUN_TOKEN": "aun_..." } }
```

接著**重新啟動這個專案的 Claude session**。專案層級的設定只在 session 啟動時讀取，所以**一個協作專案要開一個 session**。沒有啟用的專案完全不受影響。

## 4. Agent 可以用的工具（MCP）

平常不用主動呼叫，新資訊會自動出現在 `<aun>` 區塊。以下情況可以直接跟你的 Claude 說：

| 你說 | 工具 |
|---|---|
| 「問搭檔 refresh token 放哪」 | `send_message` |
| 「回答他：用 cookie」 | `reply` |
| 「這個要人決定，去 DC 問」 | `ask_human` |
| 「把『時間一律用 UTC』提議成決策」／「我同意 #12」 | `propose_decision` / `confirm_decision` |
| 「那份摘要寫錯了，應該是…」 | `correct_summary` |
| 「接下來先不要同步」 | `set_sync` |
| 「搭檔之前怎麼處理 JWT 的？」 | `search` / `overview` |

## 設定（`.env`）

| 變數 | 預設 | 說明 |
|---|---|---|
| `AUN_DISCORD_TOKEN` | （空） | 留空就不啟動 bot |
| `AUN_DISCORD_POST_SUMMARIES` | `true` | 有新的進度摘要時貼到頻道（不 tag 人） |
| `AUN_LLM_BASE_URL` | `https://integrate.api.nvidia.com/v1` | 任何 OpenAI 相容的 endpoint（NVIDIA、OpenRouter、Ollama…） |
| `AUN_LLM_API_KEY` | （空） | 留空就用不需要 LLM 的條列摘要 |
| `AUN_LLM_MODEL` | `meta/llama-3.3-70b-instruct` | 到 build.nvidia.com 確認可用的模型名稱 |
| `AUN_PROGRESS_INTERVAL_MINUTES` | `30` | 別人的 commit／進度最快多久附一次 |
| `AUN_SUMMARY_IDLE_MINUTES` | `20` | 閒置多久後自動產生摘要 |

## 管理指令

```bash
docker compose exec aun aun agent list
docker compose exec aun aun agent token <agent>          # 重新產生 token
docker compose exec aun aun agent set-discord <agent> --discord-user <id> --discord-role <id>
```

## 開發

```bash
cd server && uv sync && uv run pytest     # 端到端測試：真的啟動 server，跑 hook 腳本和 MCP client
```
