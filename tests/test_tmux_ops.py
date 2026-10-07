"""
測試 tmux_ops.py：用真的 tmux，但開在獨立的暫存 socket 上，不會碰到你正在用的 tmux。
電腦上沒有 tmux 時自動略過。

被測的「CLI」是一個小程式：每收到一行就印出「收到:<內容>」，用來確認貼上的內容完整無缺。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zuebot import tmux_ops  # noqa: E402

FAKE_CLI = "import sys\nfor line in sys.stdin:\n    print('收到:' + line.rstrip(), flush=True)\n"


@unittest.skipUnless(shutil.which("tmux"), "沒有安裝 tmux")
class TestTmuxOps(unittest.IsolatedAsyncioTestCase):
    """對真的 tmux 執行每一個操作。"""

    @classmethod
    def setUpClass(cls):
        """建立獨立的 tmux socket 資料夾、假的 CLI 程式，並故意拿掉 UTF-8 locale 模擬 launchd 環境。"""
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved_env = dict(os.environ)
        os.environ["TMUX_TMPDIR"] = cls.tmp.name
        os.environ.pop("TMUX", None)            # 測試若在 tmux 裡執行，不能連到你正在用的 server
        for key in ("LANG", "LC_ALL", "LC_CTYPE"):
            os.environ.pop(key, None)           # 模擬 launchd：沒有任何 locale
        cls.cli = Path(cls.tmp.name) / "fake_cli.py"
        cls.cli.write_text(FAKE_CLI, encoding="utf-8")
        tmux_ops.configure("tmux")              # 用新的環境重建執行器

    @classmethod
    def tearDownClass(cls):
        """關掉測試用的 tmux server、還原環境變數。"""
        asyncio.run(tmux_ops._runner.run(["kill-server"]))
        os.environ.clear()
        os.environ.update(cls.saved_env)
        tmux_ops.configure("tmux")
        cls.tmp.cleanup()

    async def asyncTearDown(self):
        """每個測試結束後關掉所有 session。"""
        for s in await tmux_ops.list_sessions():
            await tmux_ops.kill_session(s.name)

    async def new(self, name):
        """開一個跑假 CLI 的 session，等它啟動。"""
        await tmux_ops.new_session(name, self.tmp.name, f"{sys.executable} -u {self.cli}")
        await asyncio.sleep(0.5)

    async def test_empty(self):
        """沒有任何 session（甚至沒有 server）時回傳空清單，不丟例外。"""
        self.assertEqual(await tmux_ops.list_sessions(), [])
        self.assertFalse(await tmux_ops.session_exists("沒有"))

    async def test_chinese_name_without_locale(self):
        """沒有 locale 的環境下，中文名稱也要正確（v0 會變成底線）。"""
        await self.new("競賽")
        names = [s.name for s in await tmux_ops.list_sessions()]
        self.assertEqual(names, ["競賽"])
        self.assertTrue(await tmux_ops.session_exists("競賽"))

    async def test_exact_match(self):
        """demo 不存在時，不能被當成 demo2（v0 的前綴比對問題）。"""
        await self.new("demo2")
        self.assertFalse(await tmux_ops.session_exists("demo"))
        with self.assertRaises(tmux_ops.TmuxError):
            await tmux_ops.kill_session("demo")
        with self.assertRaises(tmux_ops.TmuxError):
            await tmux_ops.capture("demo")
        self.assertTrue(await tmux_ops.session_exists("demo2"))   # demo2 還活著

    async def test_paste_multiline_chinese(self):
        """多行中文貼上：每一行都要完整送達。"""
        await self.new("貼上")
        await tmux_ops.paste_text("貼上", "第一行\n第二行 有空白\n最後一行😀")
        await asyncio.sleep(0.5)
        screen = await tmux_ops.capture("貼上")
        for expected in ("收到:第一行", "收到:第二行 有空白", "收到:最後一行😀"):
            self.assertIn(expected, screen)

    async def test_pane_lookup_and_keys(self):
        """pane id 可以反查 session；按鍵只接受白名單。"""
        await self.new("查詢")
        info = (await tmux_ops.list_sessions())[0]
        self.assertTrue(info.pane_id.startswith("%"))
        self.assertEqual(await tmux_ops.session_of_pane(info.pane_id), "查詢")
        self.assertIsNone(await tmux_ops.session_of_pane("%9999"))
        self.assertIsNone(await tmux_ops.session_of_pane("不是 pane"))
        await tmux_ops.send_key("查詢", "y")
        await tmux_ops.send_key("查詢", "enter")
        await asyncio.sleep(0.3)
        self.assertIn("收到:y", await tmux_ops.capture("查詢"))
        with self.assertRaises(tmux_ops.TmuxError):
            await tmux_ops.send_key("查詢", "rm -rf /")

    async def test_name_rules(self):
        """名稱不能有冒號、句點、空白；重複名稱要被拒絕。"""
        for bad in ("a:b", "a.b", "a b", "", "x" * 31):
            with self.assertRaises(tmux_ops.TmuxError):
                await tmux_ops.new_session(bad, self.tmp.name, "true")
        await self.new("重複")
        with self.assertRaises(tmux_ops.TmuxError):
            await tmux_ops.new_session("重複", self.tmp.name, "true")


if __name__ == "__main__":
    unittest.main()
