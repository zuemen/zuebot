# zuebot

用 Telegram 訊息指揮電腦上的 Claude Code CLI：看進度、傳訊息、完成時通知、開新的 CLI。
用口語說就好（「競賽那個在幹嘛？」「跟它說改用 v2 資料集」），由你的 Claude 訂閱當「大腦」理解並執行。

- 🏠 **第一次安裝：從 [`START_HERE.md`](START_HERE.md) 開始**
- 📖 Mac 安裝教學與疑難排解：[`docs/mac-setup.md`](docs/mac-setup.md)
- 📄 需求與任務清單：[`PROMPT.md`](PROMPT.md)
- 🔎 官方文件查證筆記：[`docs/research-notes.md`](docs/research-notes.md)
- 🧪 v0 對照版（不修改）：[`reference/v0/`](reference/v0/)

## 原理

```
你（Telegram，口語或斜線指令）
   │
   ▼
bot（python -m zuebot，launchd 常駐在被控制的 Mac 上）
   ├─ 白名單、確認按鈕、機密遮蔽
   ├─ 大腦：claude -p（你的訂閱；沒有任何內建工具，只能回傳「要做什麼」）
   ├─ 工具層 tools.py：驗證每個動作，危險的先問你
   └─ tmux_ops.py：讀畫面 capture-pane、打字 paste-buffer（全專案唯一碰 tmux 的地方）
          │
          ▼
tmux 裡的 claude ──（回覆完成／需要確認）──▶ hook.py ──▶ ~/.zuebot/events.jsonl ──▶ bot 通知你
```

## 安全設計

- 只有 `ALLOWED_USER_IDS` 能操作，其他人只會收到「未授權」。
- 大腦用 `claude -p --tools ""` 執行，沒有 Bash 或任何檔案工具，只能透過工具層行動。
- 以下動作都要你按「✅ 同意」才執行（60 秒沒按自動取消）：
  - 送按鍵
  - 關閉 CLI
  - 含有危險字眼（rm、刪除、git push、--force、部署、drop…）的訊息
  - 大腦想送出「不是你原話」的內容
- 權限確認通知上的按鈕，按下時會再檢查一次畫面，確定還在權限確認畫面才送出按鍵。
- 送往 Telegram 和大腦的畫面，會先遮蔽 API key、token、私鑰、`.env` 內容。
- `new_cli` 只能在 `ALLOWED_ROOT` 底下開。

## 開發

```bash
python3 -m unittest discover -s tests -v     # 測試（用假的 claude 和獨立的 tmux socket，不影響你正在用的）
.venv/bin/python -m zuebot --debug           # 除錯模式：印出每個 tmux 指令、hook 事件、大腦呼叫
.venv/bin/python -m zuebot.selftest          # 用真的 claude 做實機自我檢查
```

## 專案結構

```
src/zuebot/
  __main__.py     啟動入口（python -m zuebot）
  config.py       讀 .env 與環境變數
  tmux_ops.py     所有 tmux 操作（全專案唯一呼叫 tmux 的地方；之後可換成 SSH 控制多台）
  screen.py       判讀 claude 畫面（信任提示、權限確認、執行中、閒置）
  cli.py          懂 claude 的操作：送字前檢查、送完確認、等待啟動
  tools.py        工具層：驗證參數、確認、歧義、原話檢查
  brain.py        大腦（claude -p）：理解口語、摘要
  safety.py       危險字眼偵測、機密遮蔽
  monitor.py      背景監控：hook 事件、完成回報、權限通知、session 消失、備援閒置偵測
  bot.py          Telegram 介面
  hook.py         Claude Code 的 hook（只用標準函式庫，相容 Python 3.9）
  setup_hooks.py  把 hook 安全地合併進 ~/.claude/settings.json
  selftest.py     實機自我檢查
bin/cc                     用 tmux 開一個 claude
scripts/                   install.sh、check_env.sh、install_launchd.sh
config/                    hooks 設定範例、launchd 範本
tests/                     測試（含假 claude、假 claude -p）
```
