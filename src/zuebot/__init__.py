"""
zuebot —— 用 Telegram 指揮 Claude Code CLI 的管家 bot。

模組分工（每個檔案只做一件事）：
  config.py    讀取 .env／環境變數
  tmux_ops.py  所有 tmux 操作（全專案唯一呼叫 tmux 的地方）
  state.py     狀態存檔（目前對象、關注清單、事件讀取進度）
  events.py    讀取 hook 寫出的事件檔
  hook.py      Claude Code 的 Stop／Notification hook（獨立執行，只用標準函式庫）
  bot.py       Telegram 指令處理
"""

__version__ = "0.1.0"
