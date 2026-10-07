"""
測試 config.py、state.py、events.py（純 Python，不需要 tmux 或 Telegram）。
執行：python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))   # 不安裝也能直接測

from zuebot import config, events, state  # noqa: E402


class TestDotenv(unittest.TestCase):
    """測試 .env 解析。"""

    def test_parse_lines(self):
        """各種寫法都要解析正確。"""
        p = config._parse_env_line
        self.assertEqual(p("A=1"), ("A", "1"))
        self.assertEqual(p("export B = hello "), ("B", "hello"))
        self.assertEqual(p('C="有 # 空白"'), ("C", "有 # 空白"))
        self.assertEqual(p("D=value  # 註解"), ("D", "value"))
        self.assertIsNone(p("# 註解"))
        self.assertIsNone(p(""))
        self.assertIsNone(p("沒有等號"))

    def test_existing_env_wins(self):
        """已經存在的環境變數不會被 .env 覆蓋。"""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text("ZUEBOT_TEST_X=from_file\nZUEBOT_TEST_Y=from_file\n", encoding="utf-8")
            os.environ["ZUEBOT_TEST_X"] = "from_env"
            try:
                self.assertEqual(config.load_dotenv(f), f)
                self.assertEqual(os.environ["ZUEBOT_TEST_X"], "from_env")
                self.assertEqual(os.environ["ZUEBOT_TEST_Y"], "from_file")
            finally:
                os.environ.pop("ZUEBOT_TEST_X", None)
                os.environ.pop("ZUEBOT_TEST_Y", None)

    def test_bad_user_ids(self):
        """user id 不是數字時要給出清楚的錯誤。"""
        self.assertEqual(config._parse_user_ids("1, 2,,3"), frozenset({1, 2, 3}))
        with self.assertRaises(config.ConfigError):
            config._parse_user_ids("123,abc")


class TestState(unittest.TestCase):
    """測試狀態存檔與讀檔。"""

    def test_roundtrip(self):
        """存檔後重新讀取，內容要一樣（模擬重開 bot）。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            s = state.State(path)
            s.set_current(42, "競賽")
            s.watch("競賽", 42)
            s.events_offset = 123
            s.save()
            s2 = state.State.load(path)
            self.assertEqual(s2.get_current(42), "競賽")
            self.assertEqual(s2.watcher("競賽"), 42)
            self.assertEqual(s2.events_offset, 123)
            s2.forget_session("競賽")
            self.assertIsNone(state.State.load(path).watcher("競賽"))
            self.assertIsNone(state.State.load(path).get_current(42))

    def test_corrupt_file(self):
        """狀態檔壞掉時不能讓 bot 起不來：備份成 .bad，用空白狀態啟動。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            path.write_text("{壞掉的 json", encoding="utf-8")
            s = state.State.load(path)
            self.assertEqual(s.watches, {})
            self.assertTrue(path.with_suffix(".json.bad").exists())

    def test_v0_compat(self):
        """可以讀 v0 的狀態檔（欄位叫 offset）。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            path.write_text(json.dumps({"current": {"1": "a"}, "watches": {"a": 1}, "offset": 7}), encoding="utf-8")
            self.assertEqual(state.State.load(path).events_offset, 7)


class TestEventReader(unittest.TestCase):
    """測試事件檔讀取。"""

    def setUp(self):
        """每個測試用一個新的暫存資料夾。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"

    def tearDown(self):
        """清掉暫存資料夾。"""
        self.tmp.cleanup()

    def append(self, raw: bytes):
        """模擬 hook 附加內容。"""
        with self.path.open("ab") as f:
            f.write(raw)

    def test_first_start_skips_old(self):
        """第一次啟動（offset=None）不補發舊事件。"""
        self.append(b'{"event":"Stop"}\n')
        r = events.EventReader(self.path, None)
        self.assertEqual(r.read_new(), [])
        self.append('{"event":"Stop","reply":"中文"}\n'.encode())
        self.assertEqual(r.read_new(), [{"event": "Stop", "reply": "中文"}])

    def test_missing_file_then_created(self):
        """事件檔還不存在時，之後新建的檔案要從頭讀。"""
        r = events.EventReader(self.path, None)
        self.assertEqual(r.read_new(), [])
        self.append(b'{"n":1}\n')
        self.assertEqual(r.read_new(), [{"n": 1}])

    def test_partial_line_not_lost(self):
        """hook 寫到一半時讀到半行，下一次要能完整讀到，不能漏掉。"""
        r = events.EventReader(self.path, 0)
        line = '{"event":"Stop","reply":"完成了"}\n'.encode()
        self.append(line[:10])
        self.assertEqual(r.read_new(), [])
        self.append(line[10:])
        self.assertEqual(r.read_new(), [{"event": "Stop", "reply": "完成了"}])

    def test_bad_line_skipped(self):
        """壞掉的行略過，其他行照常讀。"""
        r = events.EventReader(self.path, 0)
        self.append(b'not json\n{"ok":1}\n')
        self.assertEqual(r.read_new(), [{"ok": 1}])

    def test_truncated_file(self):
        """事件檔被清空後，從頭開始讀。"""
        r = events.EventReader(self.path, 0)
        self.append(b'{"a":1}\n{"a":2}\n')
        r.read_new()
        self.path.write_bytes(b'{"a":3}\n')
        self.assertEqual(r.read_new(), [{"a": 3}])

    def test_rotation(self):
        """檔案太大時輪替成 .1，之後的新事件照常讀到。"""
        old_max = events.MAX_BYTES
        events.MAX_BYTES = 10
        try:
            r = events.EventReader(self.path, 0)
            self.append(b'{"a":1}\n{"a":2}\n')
            self.assertEqual(len(r.read_new()), 2)
            self.assertTrue(self.path.with_name("events.jsonl.1").exists())
            self.assertFalse(self.path.exists())
            self.append(b'{"a":3}\n')
            self.assertEqual(r.read_new(), [{"a": 3}])
        finally:
            events.MAX_BYTES = old_max


if __name__ == "__main__":
    unittest.main()
