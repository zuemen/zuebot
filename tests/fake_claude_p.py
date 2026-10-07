#!/usr/bin/env python3
"""
fake_claude_p.py —— 測試用的假「claude -p」

  - 把收到的參數、重要環境變數、stdin 的 prompt 記到 FAKE_BRAIN_LOG（JSON lines）
  - 從 FAKE_BRAIN_SCRIPT（JSON 陣列檔）依序取出一個回應輸出；
    回應是物件就當作 structured_output，是字串就當作 result 文字
"""
import json
import os
import sys
from pathlib import Path

if "--help" in sys.argv:
    print("  --safe-mode   (假的說明文字，模擬新版 claude 支援 safe mode)")
    sys.exit(0)
prompt = sys.stdin.read()
log_path = os.environ.get("FAKE_BRAIN_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt,
                            "ZUEBOT_BRAIN": os.environ.get("ZUEBOT_BRAIN"),
                            "TMUX_PANE": os.environ.get("TMUX_PANE"), "cwd": os.getcwd()}, ensure_ascii=False) + "\n")
script = Path(os.environ["FAKE_BRAIN_SCRIPT"])
responses = json.loads(script.read_text(encoding="utf-8"))
item = responses.pop(0) if responses else "（沒有更多劇本）"
script.write_text(json.dumps(responses, ensure_ascii=False), encoding="utf-8")
if item == "__FAIL__":
    print(json.dumps({"type": "result", "is_error": True, "result": "Invalid API key · Please run /login"}))
    sys.exit(1)
if isinstance(item, dict):
    print(json.dumps({"type": "result", "is_error": False, "result": "", "structured_output": item}, ensure_ascii=False))
else:
    print(json.dumps({"type": "result", "is_error": False, "result": item}, ensure_ascii=False))
