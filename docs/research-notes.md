# 查證筆記（PROMPT.md 第 9 節）

> 2026-10 查證。官方文件與本專案假設不同的地方都記在這裡，之後的 Phase 依此實作。
> 標示「待 Mac 實測」的項目要在 Phase 1 用真的 `claude` 驗證。

## 1. 已定案：大腦用 `claude -p`（訂閱登入）

| 選項 | 登入方式 | 結論 |
|---|---|---|
| Claude Agent SDK（`claude-agent-sdk`） | 官方文件：「Anthropic 不允許第三方開發者提供 claude.ai 登入……請改用 API key」，需要 `ANTHROPIC_API_KEY`，會另外按用量計費 | ❌ 不用 |
| `claude -p --output-format json` | 一般模式可以用訂閱登入 | ✅ 採用 |

`claude -p` 的做法（Phase 2）：
- 用 `--tools ""` 關掉所有內建工具，大腦就沒有 Bash、Edit 這些能力。⚠️ `--allowedTools` 只是「預先核准」，**不會限制**只能用哪些工具，要用 `--tools` 才會真的限制。
- 用 `--json-schema` 要求大腦回傳固定格式的「要呼叫哪些工具」，結果在 `structured_output` 欄位，由 bot 驗證後自己執行。
- 呼叫時設定 `ZUEBOT_BRAIN=1`、拿掉 `TMUX_PANE`，避免大腦觸發我們的 hook，寫出假的「完成」事件。
- 在一個空的資料夾執行，避免讀到某個專案的 CLAUDE.md。`-p` 模式不會跳信任資料夾的提示。
- **轉貼原話：** 大腦只負責判斷要轉貼「哪一段」給「哪個 CLI」。bot 檢查那段文字一字不差出現在使用者原訊息裡才送出，對不上就先問使用者。

## 2. Claude Code hooks（https://code.claude.com/docs/en/hooks）

- **Stop：** 有 `session_id`、`transcript_path`、`cwd`、`hook_event_name`、`permission_mode`、`stop_hook_active`、**`last_assistant_message`**（最後回覆的文字）。
  - hook.py 以 `last_assistant_message` 為主，讀 transcript 只是備援。
  - transcript 的格式官方明說是內部格式，會隨版本改變。
- **Notification：** 有 `message`、`title`、**`notification_type`**。
  - 類型包含 `permission_prompt`（約 6 秒後發出）、`idle_prompt`（閒置約 60 秒）、`elicitation_dialog` 等。
  - matcher 可以依類型篩選。
  - bot 目前略過 `idle_prompt`，因為 Stop 時已經通知過了。
- **PermissionRequest：** 會給 `tool_name` 和 `tool_input`，可以精確轉述「它想執行什麼」。hook 不輸出就等於不做決定，選單照常顯示。**待 Mac 實測**後再加入設定。
- **執行環境：** hook 會繼承 claude 的環境變數，所以拿得到 `TMUX_PANE`。結束碼 0 而且沒有輸出是安全的；結束碼 2 會阻擋 Claude，絕對不能用。
- **信任資料夾提示：** 官方沒有文件化的跳過方式，`new_cli` 只能偵測畫面後通知使用者。

## 3. tmux（man page 與原始碼，本機 tmux 3.4 實測；Homebrew 目前是 3.7c）

| 問題 | 實測或原始碼結果 | 處理方式（`tmux_ops.py`） |
|---|---|---|
| 名稱比對 | `-t demo` 依序比對：精確、前綴、萬用字元。demo 不存在時會比對到 demo2 | session 指令用 `=名稱`；pane 指令用 `=名稱:`（沒有冒號會被當成字面上的 pane 名稱而找不到） |
| 中文名稱 | 沒有 UTF-8 locale 時，「競賽2」會變成「____2」 | 每個指令都加 `-u`，必要時補上 `LC_CTYPE=UTF-8` |
| `paste-buffer` | 預設把換行轉成 CR；`-p` 只有在程式開啟 bracketed paste 時才會包標記 | 一定要用 `-p`；CLI 還沒啟動好就貼上，多行會被拆成好幾次送出，所以要等輸入框出現 |
| `load-buffer -` | 從 stdin 讀，tmux 1.3 起就支援 | 每次用不同的 buffer 名稱 |

## 4. 把文字送進 Claude Code TUI 的已知問題（GitHub issues）

- anthropics/claude-code#91205：貼上和按鍵落在同一次輸入裡時，**貼上的文字會被丟掉** → 貼上和 Enter 分開送，中間等 0.5 秒。
- anthropics/claude-code#78177：遠端送進去的文字停在輸入框，Enter 被吃掉 → Phase 1 送完要讀畫面確認，必要時補一次 Enter。
- anthropics/claude-code#52812：某一版在 tmux 裡 Enter 全部變成換行 → `check_env.sh` 要印出 claude 的版本。
- 很長的貼上會顯示成 `[Pasted text #N +N lines]`，只是顯示方式，內容沒有少。

## 5. python-telegram-bot 22.8（需要 Python ≥ 3.10）

- 在 `post_init` 裡呼叫 `app.create_task` 不會被追蹤 → 自己保存 task，在 `post_shutdown` 取消。
- `callback_data` 最多 64 bytes → Phase 3 的確認按鈕用短 id 對照待確認的動作。
- 遇到 409 Conflict（同一個 token 有兩個程式在收訊息）會**無限重試**，不會停止 → Phase 4 要在錯誤處理器裡通知使用者。
- 單則訊息上限 4096 字；用純文字傳送，不用 Markdown，免得終端畫面的符號造成解析錯誤。

## 6. macOS 注意事項

- 系統內建的 `python3` 是 3.9，而且 hook 是由 claude 用它執行的 → hook.py 必須相容 3.9、只用標準函式庫。bot 本身要用 Homebrew 的 Python 3.10 以上。
- launchd 和非互動式 SSH 的 PATH 裡沒有 `/opt/homebrew/bin` → `.env` 的 `TMUX_BIN` 要填完整路徑。

## 7. 實作時另外發現的事（2026-10）

- **`claude -p --bare` 不能用：** `claude --help` 寫明 `--bare` 會跳過讀取鑰匙圈（keychain），macOS 上的訂閱登入就讀不到了。所以大腦改用 `--tools ""`、`--strict-mcp-config`、`--disable-slash-commands`、`--no-session-persistence` 這幾個參數來限制。
- **巢狀執行偵測：** Claude Code 會在它開出來的程式裡設定 `CLAUDECODE` 等環境變數。在 Claude Code 裡執行 `cc`、bot 或 selftest 時，新開的 claude 會繼承這些變數，可能以為自己是巢狀執行而拒絕啟動。所以開 CLI 時一律用 `env -u CLAUDECODE …` 清掉，tmux_ops 的環境也會拿掉它。
- **補按 Enter 的陷阱：** 送字後如果畫面已經變成權限選單，這時「補按 Enter」就等於選了「1. Yes」。所以只有在畫面是「閒置等輸入」時才補按，並加了回歸測試。
- **`--json-schema` 搭配 `--tools ""`：** 還沒在實機上確認 `structured_output` 一定會出現。brain.py 兩種情況都處理：沒有 `structured_output` 時，就從 `result` 文字裡解析 JSON。selftest 會記錄實際是用哪一種。
