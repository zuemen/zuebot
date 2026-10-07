#!/usr/bin/env python3
"""
hook.py —— Claude Code 的 Stop / Notification hook

Claude Code 每次「回覆完成」(Stop) 或「需要你注意」(Notification) 時會執行這支程式，
並把事件資料用 JSON 從 stdin 傳進來。這支程式只做一件事：
  把事件整理成一行 JSON，附加到 ~/.zuebot/events.jsonl，bot 會讀這個檔案並通知你。

設計原則（非常重要）：
  1. 絕對不能讓 Claude 卡住或出錯：所有例外都吞掉、不輸出任何東西、結束碼一定是 0。
  2. 只用標準函式庫、相容 Python 3.9：hook 是由 claude 用系統的 python3 執行的，
     macOS 內建的 python3 是 3.9，而且不在 bot 的虛擬環境裡。
  3. 不呼叫 tmux：只記下 TMUX_PANE（例如 %3），由 bot 透過 tmux_ops 反查是哪個 session。
     這樣不會遇到「hook 的環境沒有 UTF-8，中文名稱變成底線」的問題，
     也遵守「只有 tmux_ops.py 能呼叫 tmux」的規定。

可用的環境變數：
  ZUEBOT_EVENTS  事件檔位置（預設 ~/.zuebot/events.jsonl，要跟 bot 的設定一致）
  ZUEBOT_BRAIN   有設定時直接略過（bot 自己呼叫 claude -p 當大腦時會設，避免產生假事件）
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

MAX_TEXT_CHARS = 20000          # 單一欄位最多保留幾個字，避免事件檔被超長回覆撐爆
TRANSCRIPT_TAIL_BYTES = 2_000_000   # 讀 transcript 備援時只讀最後 2MB，大檔也很快


def events_path() -> Path:
    """事件檔位置：環境變數 ZUEBOT_EVENTS，否則 ~/.zuebot/events.jsonl。"""
    custom = os.environ.get("ZUEBOT_EVENTS")
    return Path(custom).expanduser() if custom else Path.home() / ".zuebot" / "events.jsonl"


def clip(value: object) -> str:
    """把任何值轉成字串並截斷到 MAX_TEXT_CHARS 字。"""
    text = value if isinstance(value, str) else ("" if value is None else json.dumps(value, ensure_ascii=False))
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + "…（已截斷）"


def last_assistant_text(transcript_path: str) -> str:
    """
    備援用：從對話紀錄檔（jsonl）倒著找最後一則 assistant 的文字回覆。

    只有 Stop 事件沒有提供 last_assistant_message 時才會用到。
    官方文件說明 transcript 的格式是內部格式、可能隨版本改變，所以這裡寫得很保守：
    任何看不懂的行都跳過，找不到就回傳空字串。
    """
    if not transcript_path:
        return ""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            continue
        content = (ev.get("message") or {}).get("content", [])
        if isinstance(content, str):
            if content.strip():
                return content
            continue
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(t for t in texts if t.strip())
        if text:
            return text
    return ""


def read_input() -> dict:
    """讀 stdin 的 JSON。手動在終端機執行（stdin 是鍵盤）時不等待輸入，直接回傳空字典。"""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def build_event(data: dict, pane: str) -> dict:
    """把 Claude Code 給的資料整理成 bot 需要的事件格式。"""
    event = {
        "time": time.time(),
        "event": data.get("hook_event_name", ""),        # "Stop"、"Notification" …
        "pane": pane,                                     # 例如 "%3"，bot 用它反查 session
        "cwd": data.get("cwd", ""),
        "claude_session_id": data.get("session_id", ""),
    }
    if event["event"] == "Notification":
        event["notification_type"] = data.get("notification_type", "")   # permission_prompt、idle_prompt …
        event["title"] = clip(data.get("title", ""))
        event["message"] = clip(data.get("message", ""))
    elif event["event"] == "Stop":
        # 官方文件已列出 last_assistant_message；舊版沒有的話才去讀 transcript
        reply = data.get("last_assistant_message") or last_assistant_text(data.get("transcript_path", ""))
        event["reply"] = clip(reply)
    else:
        # 其他事件（之後可能接 PermissionRequest）：保留工具名稱與參數，方便 bot 轉述
        for key in ("tool_name", "tool_input", "message"):
            if key in data:
                event[key] = clip(data[key])
    return event


def append_event(path: Path, event: dict) -> None:
    """
    把事件附加成一行。用 O_APPEND 並一次 write 整行，
    好幾個 CLI 同時觸發 hook 時，各自的行不會互相穿插。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def main() -> None:
    """hook 主流程：判斷要不要處理 → 讀輸入 → 整理 → 寫入事件檔。"""
    if os.environ.get("ZUEBOT_BRAIN"):
        return   # bot 自己的大腦觸發的，略過
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return   # 不是在 tmux 裡開的 CLI，bot 管不到，直接略過
    append_event(events_path(), build_event(read_input(), pane))


if __name__ == "__main__":
    # 先把輸出導到 /dev/null：就算有任何警告訊息，也不會被 Claude Code 當成 hook 的輸出
    try:
        sys.stdout = sys.stderr = open(os.devnull, "w")
    except Exception:
        pass
    try:
        main()
    except BaseException:
        pass   # 任何錯誤（包含 Ctrl-C）都吞掉，不影響 Claude
    os._exit(0)   # 直接以 0 結束，不執行任何可能出錯的收尾程序
