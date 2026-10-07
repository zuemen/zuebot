"""
測試 hook.py：用子行程實際執行，跟 Claude Code 呼叫它的方式一樣。
重點是「絕對不能讓 Claude 卡住」：任何輸入都要 0 結束、不輸出任何東西。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "src" / "zuebot" / "hook.py"


class TestHook(unittest.TestCase):
    """測試 hook 的輸入輸出行為。"""

    def setUp(self):
        """準備暫存的事件檔位置。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.events = Path(self.tmp.name) / "sub" / "events.jsonl"

    def tearDown(self):
        """清掉暫存資料夾。"""
        self.tmp.cleanup()

    def run_hook(self, stdin: str, pane: str | None = "%7", extra_env: dict | None = None):
        """執行 hook，回傳 (結束碼, stdout, stderr)。"""
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX_PANE", "ZUEBOT_BRAIN")}
        env["ZUEBOT_EVENTS"] = str(self.events)
        if pane:
            env["TMUX_PANE"] = pane
        env.update(extra_env or {})
        p = subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True, env=env, timeout=10)
        return p.returncode, p.stdout, p.stderr

    def read_events(self):
        """讀出事件檔裡的所有事件。"""
        return [json.loads(line) for line in self.events.read_text(encoding="utf-8").splitlines()]

    def test_stop_with_last_message(self):
        """Stop 事件：使用官方的 last_assistant_message 欄位。"""
        data = {"hook_event_name": "Stop", "cwd": "/x", "session_id": "abc", "last_assistant_message": "做完了 ✅"}
        self.assertEqual(self.run_hook(json.dumps(data)), (0, "", ""))
        ev = self.read_events()[0]
        self.assertEqual((ev["event"], ev["pane"], ev["reply"], ev["cwd"]), ("Stop", "%7", "做完了 ✅", "/x"))

    def test_stop_transcript_fallback(self):
        """Stop 事件沒有 last_assistant_message 時，改讀 transcript 的最後一則回覆。"""
        tr = Path(self.tmp.name) / "t.jsonl"
        tr.write_text("\n".join([
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "舊的"}]}}),
            json.dumps({"type": "user", "message": {"content": "hi"}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use"}, {"type": "text", "text": "新的回覆"}]}}),
        ]), encoding="utf-8")
        self.run_hook(json.dumps({"hook_event_name": "Stop", "transcript_path": str(tr)}))
        self.assertEqual(self.read_events()[0]["reply"], "新的回覆")

    def test_notification(self):
        """Notification 事件：保留類型與訊息。"""
        data = {"hook_event_name": "Notification", "notification_type": "permission_prompt", "message": "要允許嗎？"}
        self.run_hook(json.dumps(data))
        ev = self.read_events()[0]
        self.assertEqual((ev["notification_type"], ev["message"]), ("permission_prompt", "要允許嗎？"))

    def test_no_tmux_pane(self):
        """不在 tmux 裡：什麼都不寫。"""
        self.assertEqual(self.run_hook('{"hook_event_name":"Stop"}', pane=None), (0, "", ""))
        self.assertFalse(self.events.exists())

    def test_brain_skipped(self):
        """bot 自己的大腦（ZUEBOT_BRAIN=1）觸發的 hook 要略過。"""
        self.run_hook('{"hook_event_name":"Stop"}', extra_env={"ZUEBOT_BRAIN": "1"})
        self.assertFalse(self.events.exists())

    def test_garbage_never_fails(self):
        """壞掉的輸入、無法寫入的路徑：都要 0 結束、不輸出。"""
        for stdin in ["", "不是 json", "[1,2]", "{" * 10000]:
            self.assertEqual(self.run_hook(stdin), (0, "", ""), stdin[:20])
        bad = {"ZUEBOT_EVENTS": "/proc/不能寫/events.jsonl"}
        self.assertEqual(self.run_hook('{"hook_event_name":"Stop"}', extra_env=bad), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
