# zuebot

用 Telegram 訊息指揮電腦上的 Claude Code CLI：看進度、傳訊息、完成時通知、開新的 CLI。

- 📄 **需求與任務清單**：[`PROMPT.md`](PROMPT.md)
- 🧪 **已可運作的 v0（指令版）**：[`reference/v0/`](reference/v0/)

## 怎麼開始開發

```bash
git clone https://github.com/zuemen/zuebot && cd zuebot
claude
```

然後對 Claude Code 說：

> 請讀 PROMPT.md，照第 9 節開始。

## 原理

所有 CLI 都在 tmux 裡執行，bot 用 `tmux capture-pane` 讀畫面、用 `tmux paste-buffer` 打字；
Claude Code 的 Stop／Notification hook 會在「回覆完成」和「等待確認」時通知 bot。
