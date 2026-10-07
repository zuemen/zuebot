#!/usr/bin/env python3
"""
hook.py —— Claude Code 的 Stop / Notification hook

Claude Code 每次「回覆完成」(Stop) 或「在等你輸入／確認」(Notification) 時會執行這支程式，
並把事件資料用 JSON 從 stdin 傳進來。我們做的事很單純：
  1. 用環境變數 TMUX_PANE 找出「是哪個 tmux session 的 CLI」觸發的
  2. Stop 時順便抓 Claude 最後一則回覆的文字
  3. 把事件寫成一行 JSON，附加到 ~/.claude-tg/events.jsonl
bot 會一直讀這個檔案，看到新事件就通知你。

重要：這支程式絕對不能讓 Claude 卡住或出錯，所以全部包在 try 裡，且不輸出任何東西。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

EVENTS = Path(os.environ.get("CLAUDE_TG_EVENTS", str(Path.home() / ".claude-tg" / "events.jsonl")))


def last_assistant_text(transcript_path: str) -> str:
    """從對話紀錄檔(jsonl)倒著找，取出最後一則 assistant 的文字回覆"""
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") != "assistant":
            continue
        content = ev.get("message", {}).get("content", [])
        if isinstance(content, str):
            if content.strip():
                return content
            continue
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(t for t in texts if t.strip())
        if text:
            return text
    return ""


def main() -> None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return  # 不是在 tmux 裡開的 CLI，bot 管不到，直接略過

    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    # 用 pane id 反查 tmux session 名稱（例如「競賽」）
    try:
        session = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#S"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        session = ""

    event = {
        "time": time.time(),
        "event": data.get("hook_event_name", ""),   # "Stop" 或 "Notification"
        "session": session,
        "pane": pane,
        "cwd": data.get("cwd", ""),
        "message": data.get("message", ""),         # Notification 的提示文字
    }
    if event["event"] == "Stop":
        # 新版可能直接提供最後回覆；沒有的話就去讀對話紀錄檔
        event["reply"] = data.get("last_assistant_message") or last_assistant_text(data.get("transcript_path", ""))

    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # 任何錯誤都吞掉，不影響 Claude
    sys.exit(0)
