"""
fake_claude.py —— 測試用的「假 claude」：模擬 Claude Code 畫面上的關鍵元素

  - 環境變數 FAKE_TRUST=1：啟動時先顯示「信任資料夾」提示，等你按 Enter
  - 一般狀態：顯示輸入框（│ > ）與「? for shortcuts」
  - 收到含有 ASKPERM 的訊息：顯示權限確認選單，等你按鍵
  - 每收到一則訊息就模擬 Stop hook（如果有設定 FAKE_HOOK＝hook.py 路徑）
"""
import json
import os
import subprocess
import sys

BOX_TOP = "╭" + "─" * 40 + "╮"
BOX_BOTTOM = "╰" + "─" * 40 + "╯"


def show_prompt():
    """畫出輸入框。"""
    print(BOX_TOP)
    print("│ > ", end="", flush=True)


def fire_hook(event):
    """模擬 Claude Code 執行 hook。"""
    hook = os.environ.get("FAKE_HOOK")
    if hook:
        subprocess.run([sys.executable, hook], input=json.dumps(event), text=True)


if os.environ.get("FAKE_TRUST") == "1":
    print("Do you trust the files in this folder?")
    print("❯ 1. Yes, proceed")
    print("  2. No, exit")
    print("Enter to confirm · Esc to exit", flush=True)
    sys.stdin.readline()

print("✻ Welcome to Fake Claude Code!")
show_prompt()
for line in sys.stdin:
    line = line.rstrip("\n")
    print(BOX_BOTTOM)
    print("  ? for shortcuts")
    print(f"● 收到：{line}", flush=True)
    if "ASKPERM" in line:
        print("Bash command\n  echo hi\nDo you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No (esc)", flush=True)
        answer = sys.stdin.readline().strip()
        print(f"● 權限回答：{answer!r}", flush=True)
    fire_hook({"hook_event_name": "Stop", "last_assistant_message": f"已處理：{line}"})
    show_prompt()
