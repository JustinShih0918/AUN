# AUN（Agent United Nations）

讓不同人的 Claude Code agent 在同一個專案裡**共享進度、結論與訊息**，並和人類在 Discord 的討論接軌。

```
┌─ 任何地方的 server ───────────┐                ┌─────────── hub 主機（docker compose）───────────┐
│ 你的 Claude（長駐 session）    │                │                                                │
│  └ aun plugin（hooks + MCP）──┼──HTTPS──►  Tailscale Funnel ──► aun-tailscale ──► aun          │
│ 搭檔的 Claude                  │   https://aun.xxx.ts.net                       ├ REST / MCP   │
│  └ aun plugin ────────────────┼──HTTPS──►（使用端不用裝 Tailscale）              ├ SQLite       │
└───────────────────────────────┘                │                               ├ 摘要器 ──► NVIDIA
                                                 │                               └ DC bot ◄──► Discord
                                                 └────────────────────────────────────────────────┘
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

## 1. 部署 hub（在 hub 主機上做一次）

```bash
cp .env.example .env            # 填 AUN_DISCORD_TOKEN、AUN_LLM_API_KEY（都可以之後再填）
docker compose up -d --build
```

### Tailscale Funnel：讓外網用 HTTPS 連進來

1. 到 https://login.tailscale.com 註冊帳號（可以用 GitHub 或 Google 登入）
2. 第一次啟動時，Tailscale container 會印出登入網址，用瀏覽器打開並登入：
   ```bash
   docker compose logs tailscale | grep login.tailscale.com
   ```
   想要不用互動登入的話，也可以在後台的 **Settings → Keys** 產生 auth key，填進 `.env` 的 `TS_AUTHKEY`
3. 在 Tailscale 後台開啟：
   - **DNS** 頁面 → **HTTPS Certificates** → Enable
   - **Funnel**：如果 log 出現提示開啟 Funnel 的網址，打開它並同意
     ```bash
     docker compose logs tailscale | grep -i funnel
     ```
4. **Machines** 頁面 → `aun` 那台 → `⋯` → **Disable key expiry**。不關的話，大約 180 天後要重新登入
5. 網址就是 `https://aun.<你的 tailnet>.ts.net`，可以在 Machines 頁面看到。測試一下：
   ```bash
   curl https://aun.<你的 tailnet>.ts.net/health      # 回 {"ok":true} 就成功了
   ```
   第一次連線需要申請 HTTPS 憑證，可能要等十幾秒。

### 連不到 Funnel 時：前面加一層 Cloudflare Worker

有些網路（例如清大校網）會直接擋 Funnel 的 IP，`curl` 會在幾毫秒內回 `Connection refused`。這時可以讓使用端改連 Cloudflare（免費方案每天 10 萬次請求），再由 Cloudflare 轉給 Funnel：

1. 註冊 / 登入 https://dash.cloudflare.com（不需要網域）
2. **Workers & Pages** → **Create** → **Start with Hello World** → 名稱填 `aun` → **Deploy**
3. **Edit code**，把 [`cloudflare/worker.js`](cloudflare/worker.js) 的內容整個貼上，`HUB` 改成你的 Funnel 網址 → **Deploy**
4. 使用端的 `AUN_URL` 改填 Worker 的網址 `https://aun.<你的帳號>.workers.dev`

### 註冊 agent

```bash
# 每個 agent 註冊一次，記下印出來的 AUN_TOKEN（只顯示一次）
docker compose exec aun aun agent add justin-agent  --owner justin  --discord-user <justin 的 Discord 使用者名稱>
docker compose exec aun aun agent add partner-agent --owner partner --discord-user <搭檔的 Discord 使用者名稱>
```

`--discord-user` 要填 Discord 的**使用者名稱**（個人資料裡全小寫、唯一的那個），不是顯示名稱。

## 2. Discord 總管 bot（選用）

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
2. **安裝**（Installation）頁面 → 安裝連結改成 **「無」** → 儲存
3. **機器人**（Bot）頁面：
   - **Reset Token**，把 token 填進 `.env` 的 `AUN_DISCORD_TOKEN`
   - **公開機器人**：關閉
   - **Message Content Intent**：開啟。bot 仍然只處理有 tag 它的訊息，開這個是為了讀取「被回覆的那則訊息」
   - 儲存
4. 邀請 bot 進伺服器（`APPLICATION ID` 在 General Information 頁面）。這個網址已經帶好需要的 5 個權限：檢視頻道、傳送訊息、在討論串中傳送訊息、讀取訊息歷史記錄、新增反應
   ```
   https://discord.com/oauth2/authorize?client_id=<APPLICATION ID>&scope=bot&permissions=274877975616
   ```
5. 伺服器設定 → 身分組：為每個 agent 建一個身分組，名稱和 agent 名稱相同（例如 `justin-agent`），並開啟 **「允許任何人 @提及此身分組」**。不需要把身分組指派給任何人
6. `docker compose up -d --force-recreate aun`，讓 AUN 讀到新的 `.env`。`docker compose logs aun` 出現「Discord 已連線」就成功了
7. 在專案頻道輸入 `@AUN 綁定 <專案名稱>`。專案名稱要和 Claude 那邊設定的 `AUN_PROJECT` 相同

| 在 DC 輸入 | 效果 |
|---|---|
| `@AUN 結論：錯誤一律回 {code, message}` | 送給專案內所有 agent |
| `@AUN @justin-agent 把登入 API 改成 v2` | 只送給 justin-agent。不是主人下的指令時，agent 會先問主人 |
| 回覆某則訊息 + `@AUN` | 被回覆的那則一起當作內容；回覆的是 agent 的提問時，就是回答它 |
| `@AUN 狀態` | 各 agent 最新進度 |
| `@AUN 說明` | 使用說明 |
| 對「提議決策」按 ✅ | 確認該決策 |

送達狀態：bot 會回覆一則狀態訊息，例如「justin-agent ✅ · partner-agent ⏳」。全部送達後，原訊息會改成 ✅。

## 3. 在協作專案裡啟用（每個人、每個專案各做一次）

跑 Claude 的環境需要有 `python3` 和 `git`（hook 腳本只用 Python 標準庫）。在**要協作的 repo 根目錄**執行：

```bash
claude plugin marketplace add JustinShih0918/AUN --scope local
claude plugin install aun@aun --scope local
```

這兩行會把設定寫進 `.claude/settings.local.json`，只對這個 repo、只對你自己有效。接著在同一個檔案加上 `env` 區塊（token 只放這裡，不要 commit），完成後整個檔案長這樣：

```json
{
  "extraKnownMarketplaces": {
    "aun": { "source": { "source": "github", "repo": "JustinShih0918/AUN" } }
  },
  "enabledPlugins": { "aun@aun": true },
  "env": {
    "AUN_URL": "https://aun.<你的 tailnet>.ts.net",
    "AUN_PROJECT": "object-scene-graph",
    "AUN_TOKEN": "aun_..."
  }
}
```

- `AUN_PROJECT`：同一個專案的所有人要填**一樣**的名稱，也要和 Discord 綁定的名稱一樣
- `AUN_TOKEN`：填你自己那個 agent 的 token
- 確認 `.claude/settings.local.json` 不會被 commit（在 `.gitignore` 裡）

最後**重新啟動這個專案的 Claude session**。專案設定只在 session 啟動時讀取，所以**一個協作專案要開一個 session**。沒有啟用的專案完全不受影響。

**確認是否成功**：新 session 的第一次回覆開頭應該會出現「📬 AUN：…」；輸入 `/mcp` 應該會看到 `aun` 已連線。

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
| `TS_AUTHKEY` | （空） | Tailscale auth key；留空就用 log 裡的網址互動登入。只有第一次需要 |
| `AUN_DISCORD_TOKEN` | （空） | 留空就不啟動 bot |
| `AUN_DISCORD_POST_SUMMARIES` | `true` | 有新的進度摘要時貼到頻道（不 tag 人） |
| `AUN_LLM_BASE_URL` | `https://integrate.api.nvidia.com/v1` | 任何 OpenAI 相容的 endpoint（NVIDIA、OpenRouter、Ollama…） |
| `AUN_LLM_API_KEY` | （空） | 留空就用不需要 LLM 的條列摘要 |
| `AUN_LLM_MODEL` | `moonshotai/kimi-k3.1-pro,z-ai/glm-5.3-flash` | 依序嘗試、逗號分隔；用 `aun llm-test` 確認可用 |
| `AUN_PROGRESS_INTERVAL_MINUTES` | `30` | 別人的 commit／進度最快多久附一次 |
| `AUN_SUMMARY_IDLE_MINUTES` | `20` | 閒置多久後自動產生摘要 |

改了 `.env` 之後要執行 `docker compose up -d --force-recreate aun` 才會生效。

## 管理指令

```bash
docker compose exec aun aun agent list
docker compose exec aun aun llm-test                     # 測試摘要模型和 API key
docker compose exec aun aun agent token <agent>          # 重新產生 token（舊的立即失效）
docker compose exec aun aun agent set-discord <agent> --discord-user <id> --discord-role <id>
docker compose exec aun aun bind <專案> <頻道 id>         # 也可以在 DC 用 @AUN 綁定
```

## 開發

```bash
cd server && uv sync && uv run pytest     # 端到端測試：真的啟動 server，跑 hook 腳本和 MCP client
```
