# zuebot

用 Telegram 訊息指揮電腦上的 Claude Code CLI：看進度、傳訊息、完成時通知、開新的 CLI。

- 📄 **需求與任務清單**：[`PROMPT.md`](PROMPT.md)
- 🔎 **官方文件查證筆記**：[`docs/research-notes.md`](docs/research-notes.md)
- 🧪 **v0 對照版（不修改）**：[`reference/v0/`](reference/v0/)

## 原理

```
你（Telegram）──▶ bot（python -m zuebot，跑在被控制的 Mac 上）
                     │  讀畫面：tmux capture-pane　　打字：tmux paste-buffer
                     ▼
                 tmux 裡的 claude ──（回覆完成／等你確認）──▶ hook.py ──▶ ~/.zuebot/events.jsonl
                                                                              │
                 bot 每 2 秒讀一次事件檔，有你關注的 CLI 就通知你 ◀──────────────┘
```

## 快速開始（目前是 Phase 0：斜線指令版）

> 完整的 Mac 安裝教學（Homebrew、防止睡眠、開機自動啟動）會在 Phase 1 寫在 `docs/mac-setup.md`。

**1. 下載專案、建立虛擬環境**
```bash
cd ~
git clone https://github.com/zuemen/zuebot && cd zuebot
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```
預期最後一行看到 `Successfully installed ... zuebot-0.1.0`。

**2. 建立 Telegram bot、填設定**
- 在 Telegram 找 **@BotFather**，傳 `/newbot`，依指示取名，拿到一串 token。
- 執行 `cp .env.example .env`，用編輯器打開 `.env`，把 token 填進 `TELEGRAM_BOT_TOKEN`。

**3. 取得你的 user id**
```bash
python -m zuebot
```
在 Telegram 傳任何訊息給你的 bot，它會回覆「未授權。你的 user id 是 123456789」。
把這個數字填進 `.env` 的 `ALLOWED_USER_IDS`，按 `Ctrl-C` 停掉 bot 再重新啟動。

**4. 設定 Claude Code 的 hook**

把 [`config/claude_settings_hooks.json`](config/claude_settings_hooks.json) 的 `"hooks"` 內容合併進 `~/.claude/settings.json`。
如果你的專案不是放在 `~/zuebot`，要把裡面的路徑改掉。

**5. 用 `cc` 開一個被管理的 CLI**
```bash
~/zuebot/bin/cc 測試 ~/projects/demo
```
會進入 claude 的畫面。按 `Ctrl-b` 再按 `d` 離開畫面，claude 會在背景繼續跑。

**6. 在 Telegram 試試看**

傳 `/list` 應該會看到「測試」；傳 `/use 測試` 之後直接打字，就會送進那個 CLI，完成時 bot 會通知你。

## 除錯

```bash
python -m zuebot --debug    # 印出每個 tmux 指令和 hook 事件
python -m unittest discover -s tests -v    # 跑測試（tmux 測試會開在獨立的 socket，不影響你正在用的）
```

## 專案結構

```
src/zuebot/
  __main__.py   啟動入口（python -m zuebot）
  config.py     讀 .env 與環境變數
  tmux_ops.py   所有 tmux 操作（全專案唯一呼叫 tmux 的地方）
  state.py      狀態存檔（目前對象、關注清單）
  events.py     讀 hook 事件檔
  hook.py       Claude Code 的 hook（只用標準函式庫，相容 macOS 內建的 Python 3.9）
  bot.py        Telegram 指令
bin/cc                                用 tmux 開一個 claude
config/claude_settings_hooks.json     要合併進 ~/.claude/settings.json 的 hooks
tests/                                測試
```
