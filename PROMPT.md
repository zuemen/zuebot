# zuebot —— 用 Telegram 指揮 Claude Code CLI 的管家 Agent

> **給 Claude Code 的完整需求說明與任務清單。**
> 請先完整讀完本文件，再依第 9 節「開始前請先做的事」動手。
> 本文件與官方文件（Claude Code hooks、Claude Agent SDK、python-telegram-bot、tmux）不一致時，以官方文件為準，並明確告訴我差異在哪裡。

---

## 0. 一句話說明

我同時開好幾個 Claude Code CLI 處理不同專案。我要一個 **Telegram bot 管家**，讓我人不在電腦前，也能用手機訊息**看它們跑到哪、把話傳給它們、在它們跑完或卡住時收到通知、甚至開新的 CLI 去處理新問題**。

---

## 1. 我的痛點

- 我同時開好幾個 CLI（競賽專案、MedSSI、研究程式……），離開電腦就完全不知道哪個跑到哪、有沒有卡住、是不是在等我回答。
- 某個 CLI 跑完了，我要回到電腦前才會發現，浪費等待時間。
- 想補一句指示（例如「改用另一個資料集」），也得回到電腦前打字。
- 突然想到新問題，想另外開一個 CLI 處理，人不在電腦前就做不到。

我要的不是另一個聊天機器人，而是一個**站在所有 CLI 外面的管家**，概念類似 OpenClaw 這類個人 agent，但架構和權限範圍由我自己掌握。

---

## 2. 已經定案的決策（不需要再討論）

| 項目 | 決定 | 理由 |
|---|---|---|
| 訊息平台 | **Telegram** | 免費主動推播、不限則數；用 polling，不需要公開網址；有 inline 按鈕可做「允許／拒絕」確認 |
| 部署方式 | **一台電腦一個 bot**（每台電腦各自在 BotFather 開一個 bot、各自用自己的 token） | 同一個 bot token 只能有一個程式在收訊息，多台共用會出現 `409 Conflict`。第一個目標是我的**另一台 Mac** |
| 怎麼控制 CLI | **所有被管的 CLI 都跑在 tmux 裡** | 一般終端機視窗外部讀不到、也打不進去；tmux 可以 `capture-pane` 讀畫面、`paste-buffer` 打字 |
| 「跑完了沒」怎麼判斷 | **Claude Code 的 Stop hook**（主要），**Notification hook** 判斷「在等我確認」，讀畫面當備援 | 只靠畫面猜不可靠 |
| 語言與作業系統 | Python 3、macOS（Homebrew 的 tmux） | — |
| 大腦的實作 | **`claude -p`，用我的 Claude 訂閱登入**（2026-10 定案） | Agent SDK 依官方文件必須用 API key、另外計費；`claude -p` 可以用訂閱，並用 `--tools ""` 關掉所有內建工具 |
| 轉貼「原話」 | 我說話後由大腦判斷要不要轉貼、轉給誰，但**送進 CLI 的必須是我的原文**，不能是大腦改寫的版本（2026-10 定案） | 避免大腦改寫後意思走樣。大腦只指出要轉貼哪一段，bot 驗證那段是原訊息的子字串才送出，對不上就先問我 |
| 機器配置 | 被控制的是 **Mac 桌機**（bot 裝在這台）；我用筆電或手機上的 Telegram 操作 | 多台互傳資料與集中管理留到 Phase 5 |

**之後才做（現在不做）：** 多台電腦集中管理（一個中央 bot＋Tailscale＋SSH 控制各台）。設計時請保留擴充空間，例如把 tmux 操作集中在一個模組裡，之後只要改成 `ssh 機器 tmux …`；但現在不要實作。

---

## 3. 目前進度：v0 已經寫好（在 `reference/v0/`）

| 檔案 | 用途 |
|---|---|
| `tmux_bot.py` | 指令版 Telegram bot：`/list` `/use` `/look` `/send` `/paste` `/watch` `/unwatch` `/key` `/new` `/kill`，直接打字會送進「目前對象」 |
| `hook.py` | Stop／Notification hook：用 `TMUX_PANE` 環境變數反查是哪個 tmux session，Stop 時抓最後一則回覆，寫入 `~/.claude-tg/events.jsonl` |
| `cc` | `cc <名稱> [資料夾]`：用 tmux 一行開好一個 CLI |
| `claude_settings_hooks.json` | 要併進 `~/.claude/settings.json` 的 hooks 設定 |

**已驗證：** 在 Linux 上用真的 tmux 加上一個模擬的 CLI 跑過一輪：貼上訊息、讀畫面、hook 寫事件、bot 讀到事件後送出「✅ 這輪完成」通知，全部正常。

**尚未驗證（Phase 1 要處理）：**
- 在 macOS 上搭配**真的 `claude`** 執行
- Stop hook 實際提供的欄位（`last_assistant_message` 是否存在、`transcript_path` 在 Stop 觸發當下是否已寫入最後一則回覆）
- Claude Code 的 TUI 對 `tmux paste-buffer -p`（bracketed paste）加 Enter 的反應，包含多行文字與中文
- 第一次在某資料夾開 `claude` 時的「信任資料夾」提示，以及權限確認選單的按鍵操作

---

## 4. 我想要的使用體驗（最終驗收標準）

我在 Telegram 用**一般口語**說話，agent 要能理解並執行，不需要我記指令格式。v0 的斜線指令保留當作備用。

| 我說的話 | Agent 應該做的事 |
|---|---|
| 「幫我關注終端 CLI 目前在跑競賽的那個，看跑到哪裡，結束請回報給我」 | 找出名稱或工作目錄最符合「競賽」的 CLI → 讀畫面並用 3–5 句話摘要進度 → 開始關注 → 這一輪完成時主動回報結果摘要 |
| 「幫我貼上接下來這段訊息」＋下一則訊息 | 記住下一則是要轉貼的內容 → 原封不動送進目標 CLI → 回覆「已送出」→ 自動關注，完成時回報 |
| 「medssi 那個現在在幹嘛？」 | 讀畫面，摘要狀態（執行中／閒置／在等我確認某件事） |
| 「現在有哪些 CLI 在跑？」 | 列出所有 CLI、工作目錄、狀態 |
| 「開一個新的 CLI 在 ~/projects/thesis，幫我整理 related work」 | 開新的 tmux session、啟動 claude、處理信任提示、送出任務、自動關注 |
| 「競賽那個如果在問要不要允許，就幫我按允許」 | 讀畫面確認真的是權限確認 → **先把它在問什麼轉述給我，等我按「同意」按鈕才送出按鍵** |
| 「停止關注競賽那個」 | 取消關注 |

**指稱有歧義時**（例如兩個 CLI 名字都像「競賽」），要列出候選讓我選，**不可以自己猜一個就執行**。

### 回報品質
- 要像懂技術的助理在跟我報告，不要把終端畫面原樣丟給我。
  - 好：「競賽那個在跑第 3 個 epoch，loss 從 0.82 降到 0.41，沒有錯誤，預估還要 10 分鐘。」
  - 不好：直接貼 80 行終端輸出。
- 完成回報要包含：做了什麼、結果、有沒有錯誤、有沒有需要我決定的事。
- 我說「給我原文」時才附畫面原文（太長自動分段）。
- 一律使用**繁體中文**。

---

## 5. 架構

```
我（Telegram，自然語言 或 斜線指令）
   │
   ▼
Bot 主程式（Python，常駐，跑在被控制的那台 Mac 上）
   ├─ 收發訊息、白名單驗證、inline 確認按鈕
   ├─ 狀態：目前對象、關注清單、待轉貼、待確認動作（存檔）
   ├─ 事件迴圈：讀 hook 事件 → 通知
   └─ 大腦：把口語轉成工具呼叫
          │
          ▼
工具層（大腦只能透過這些工具行動；工具自己再驗證參數）
   ├─ list_clis()
   ├─ read_screen(name, lines)
   ├─ send_text(name, text)
   ├─ send_key(name, key)          ← 需我確認
   ├─ watch(name) / unwatch(name)
   ├─ new_cli(name, cwd, first_prompt?)
   ├─ arm_paste(name)
   └─ kill_cli(name)               ← 需我確認
          │
          ▼
tmux（集中在單一模組，之後可換成 ssh 遠端執行）
```

### 大腦的實作
- **已定案：用 `claude -p`（訂閱登入）**，查證細節見 `docs/research-notes.md`。以下是原本的評估說明，保留作紀錄。
- 優先評估 **Claude Agent SDK（Python）**，把上面的工具註冊成自訂工具。實作前請查官方文件，確認能否用我的 Claude 訂閱登入、還是需要 API key，並在 README 寫清楚。
- 如果 SDK 不適合，替代方案是 `claude -p --output-format json` 加上工具描述，由 bot 解析大腦回傳的 JSON 動作後自己執行。兩者擇一，並說明理由。
- **禁止**讓大腦擁有不受限的 Bash 權限；它只能呼叫工具層。
- 摘要畫面或回覆時也由大腦負責，產出第 4 節要求的報告風格。

---

## 6. 安全規則（必須遵守）

1. **白名單**：只有 `ALLOWED_USER_IDS` 能操作，其他人一律不回應任何系統資訊（只回「未授權」與對方 user id）。
2. **需要我確認才執行**（用 Telegram inline 按鈕「✅ 同意／❌ 取消」，60 秒逾時自動取消）：
   - 在權限確認畫面送出任何按鍵
   - 送出的訊息包含 `rm`、刪除、`git push`、`--force`、`reset --hard`、部署、`drop` 等危險字眼
   - 關閉 tmux session
3. **路徑限制**：`new_cli` 只能在 `ALLOWED_ROOT` 底下開。
4. **遮蔽機密**：回傳的畫面或摘要中，疑似 API key、token、私鑰、`.env` 內容的字串要遮蔽。
5. 設定一律從環境變數或 `.env` 讀取；提供 `.env.example`，`.gitignore` 排除 `.env`、狀態檔、事件檔。
6. hook 腳本**絕對不能讓 Claude 卡住或報錯**：所有例外都要吞掉、不輸出任何東西、結束碼為 0。

---

## 7. 任務清單

請**依序**完成，每完成一個 Phase 就 commit 一次（commit 訊息用繁體中文），並在本節把 `[ ]` 改成 `[x]`。

### Phase 0：專案骨架
- [x] 建立 `src/zuebot/`，把 v0 的程式搬進來並拆成模組：`tmux_ops.py`（所有 tmux 操作）、`state.py`、`events.py`、`bot.py`（Telegram handlers）、`hook.py`
- [x] `requirements.txt`、`.env.example`、`.gitignore`
- [x] `bin/cc` 腳本、`config/claude_settings_hooks.json`
- [x] 啟動方式：`python -m zuebot`（自動讀 `.env`）
- [x] 保留 `reference/v0/` 不動，當作對照

### Phase 1：在 Mac 上用真的 claude 驗證 v0 功能
> 另外新增：`scripts/install.sh`（一鍵安裝）、`python -m zuebot.setup_hooks`（自動合併 hook 設定）、`python -m zuebot.selftest`（實機自我檢查並產生報告）。
- [x] 寫一份 `docs/mac-setup.md`：安裝 Homebrew／tmux／Python venv、建 bot、取得 user id、合併 hooks 設定、啟動、防止睡眠
- [x] 寫 `scripts/check_env.sh`：檢查 tmux、claude、python 版本、hooks 是否已設定、`.env` 是否齊全，逐項印出 ✅／❌
- [x] 查證 Stop／Notification hook 的實際輸入欄位，修正 `hook.py`；若 Stop 當下 transcript 還沒寫完，要加短暫重試（官方文件已查證；⏳ 實機欄位由 `python -m zuebot.selftest` 記錄）
- [x] 驗證 bracketed paste 在 Claude Code TUI 的行為（單行、多行、中文、很長的文字），必要時調整等待時間或改用其他送字方式（送完自動檢查、必要時補按 Enter；⏳ 實機由 selftest 驗證）
- [x] 處理「信任此資料夾」提示：`new_cli` 要偵測到這個畫面並告訴我，而不是卡住
- [x] 加入 `--debug` 模式，把每個 tmux 指令和 hook 事件印到 log

### Phase 2：自然語言大腦
- [x] 實作工具層（第 5 節），每個工具都要驗證參數
- [x] 接上大腦（Agent SDK 或 `claude -p`，見第 5 節），非斜線開頭的訊息交給大腦處理
- [x] 原話轉貼：大腦判斷要轉貼時只回傳「哪一段、給誰」，bot 驗證是原訊息的子字串才原文送出（見第 2 節）
- [x] 支援「目前對象」：如果我已經 `/use` 某個 CLI，「直接傳話給它」和「問大腦」要能區分（建議：預設交給大腦；以 `>` 開頭的訊息直接原文送進目前對象）
- [x] 歧義處理：候選多於一個時列出選項讓我選（inline 按鈕）
- [x] 進度摘要與完成回報改由大腦產生（第 4 節的品質要求）
- [x] 大腦本身的錯誤或逾時要回報一句人話，並提示可改用斜線指令

### Phase 3：安全與確認
- [x] Inline 按鈕確認機制（第 6 節第 2 點），含 60 秒逾時
- [x] 危險字眼偵測
- [x] 機密遮蔽
- [x] Notification 事件附上畫面最後幾行，以及「允許／拒絕」按鈕（按了才送鍵）
  > 實作說明：按鈕依畫面上實際的選項產生；按下時會再確認畫面仍是權限確認才送鍵，所以有效時間預設 10 分鐘（`PERMISSION_BUTTON_TIMEOUT`），其他確認按鈕維持 60 秒。

### Phase 4：穩定性
- [ ] 狀態（目前對象、關注清單）存檔，重開 bot 能恢復
- [ ] tmux session 被手動關掉時，自動從關注清單移除並通知我一次
- [ ] 備援偵測：被關注的 CLI 若沒有 hook 事件，每 30 秒比對畫面，連續兩次沒變且出現輸入提示時視為閒置並回報（可在 `.env` 關閉）
- [ ] `/paste` 5 分鐘逾時提醒
- [ ] macOS 開機自動啟動：提供 `launchd` 的 plist 範本與安裝說明
- [ ] 任何未預期錯誤都要通知我一句人話，bot 本身不能默默掛掉

### Phase 5（之後，現在不做）：多台電腦
- 中央 bot 加上 Tailscale，透過 SSH 執行 `tmux_ops`；工具多一個 `machine` 參數。只要確保 Phase 0 的 `tmux_ops.py` 是唯一呼叫 tmux 的地方即可。

---

## 8. 程式風格

- 提供**完整、可直接執行**的程式碼，不要只有片段或概念。
- 每個函式和關鍵段落都要有**中文註解**，說明「在做什麼」和「為什麼這樣做」。
- README 與文件要寫給**初學者**看：逐步、每一步附上要打的指令和預期看到的結果。
- 能用標準函式庫就不要加套件；不要過度設計。
- 每個 Phase 結束時，用繁體中文告訴我：做了什麼、怎麼測、還有什麼沒解決。

---

## 9. 開始前請先做的事

1. 用你自己的話複述你理解的需求、已定案的決策和架構（簡短即可）。
2. 讀 `reference/v0/` 的程式，列出你看到的問題或風險。
3. 查證要用到的官方文件（Claude Code hooks、Claude Agent SDK、python-telegram-bot、tmux `paste-buffer`），列出跟本文件假設不同的地方。
4. 提出 Phase 0 的檔案結構，**等我確認後**再開始寫程式。

---

## 10. 最終驗收測試

1. 在 Mac 上 `cc 測試 ~/projects/demo` 開一個 CLI，在 Telegram 說「現在有哪些 CLI」，能看到「測試」。
2. 說「測試那個在幹嘛」，得到摘要，而不是原始畫面。
3. 說「幫我貼上接下來這段訊息」，再傳一段**多行**文字，CLI 收到完整內容且只送出一次。
4. 說「關注測試那個，結束跟我說」，CLI 完成後 10 秒內收到完成回報，內容是摘要。
5. 說「開一個新的 CLI 在 ~/projects/demo2，幫我建立一個 hello.py」，新 session 出現、任務被執行、完成時收到回報。
6. CLI 跳出權限確認時，收到 🔔 通知和按鈕；按「同意」後 CLI 繼續執行。
7. 傳「幫我在競賽那個執行 git push --force」，bot 先要求確認，不會直接送出。
8. 用非白名單帳號傳訊息，bot 不洩漏任何系統資訊。
9. 兩個名稱相近的 CLI 時說「關注 demo 那個」，會列出候選讓我選。
10. 重開 bot（或重開機後 launchd 自動啟動），關注清單仍在。
