"""測試 setup_hooks.py：合併 hook 時不能弄壞使用者原本的設定。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zuebot import setup_hooks  # noqa: E402


class TestSetupHooks(unittest.TestCase):
    """安裝、重複安裝、移除、壞掉的設定檔。"""

    def setUp(self):
        """準備一個含有其他設定與其他 hook 的假設定檔。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"
        self.original = {
            "theme": "dark",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]},
        }
        self.path.write_text(json.dumps(self.original), encoding="utf-8")

    def tearDown(self):
        """清掉暫存資料夾。"""
        self.tmp.cleanup()

    def run_main(self, *args):
        """執行命令列入口。"""
        return setup_hooks.main([*args, "--settings", str(self.path)])

    def load(self):
        """讀回設定檔。"""
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_install_keeps_other_settings(self):
        """安裝後，原本的設定和其他 hook 都還在；重複安裝不會變多。"""
        self.assertEqual(self.run_main("--check"), 1)
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.run_main(), 0)    # 重複安裝
        data = self.load()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["hooks"]["PreToolUse"], self.original["hooks"]["PreToolUse"])
        stop_cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn("say done", stop_cmds)
        self.assertEqual(sum("zuebot" in c for c in stop_cmds), 1)
        self.assertEqual(self.run_main("--check"), 0)
        self.assertTrue(list(Path(self.tmp.name).glob("settings.json.bak-*")))   # 有備份

    def test_remove(self):
        """移除只刪 zuebot 的 hook。"""
        self.run_main()
        self.run_main("--remove")
        self.assertEqual(self.load(), self.original)

    def test_broken_file_untouched(self):
        """設定檔格式壞掉時不能覆蓋它。"""
        self.path.write_text("{壞掉", encoding="utf-8")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{壞掉")

    def test_missing_file(self):
        """設定檔不存在時直接建立。"""
        self.path.unlink()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(set(self.load()["hooks"]), set(setup_hooks.EVENTS))


if __name__ == "__main__":
    unittest.main()
