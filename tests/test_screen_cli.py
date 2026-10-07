"""
測試 screen.py（畫面判讀）與 cli.py（送字、等待啟動、信任提示）。
cli 的測試用 tests/fake_claude.py 模擬 claude 的畫面，跑在獨立的 tmux socket 上。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zuebot import cli, screen, tmux_ops  # noqa: E402

FAKE = Path(__file__).with_name("fake_claude.py")

TRUST_SCREEN = """
 Do you trust the files in this folder?

 /Users/me/projects/demo

 Claude Code may read files in this folder.

 ❯ 1. Yes, proceed
   2. No, exit

 Enter to confirm · Esc to exit
"""
TRUST_SCREEN_NEW = """
 Accessing workspace:
 /Users/me/projects/demo
 Quick safety check: Is this a project you created or one you trust?
 ❯ 1. Yes, I trust this folder
   2. No, exit
"""
PERMISSION_SCREEN = """
● I'll run the command.
╭──────────────────────────────────────╮
│ Bash command                         │
│   rm -rf build                       │
│ Do you want to proceed?              │
│ ❯ 1. Yes                             │
│   2. Yes, and don't ask again        │
│   3. No, and tell Claude what to do differently (esc) │
╰──────────────────────────────────────╯
"""
IDLE_BOX = """
● 完成了。
╭──────────────────────────────────────╮
│ >                                    │
╰──────────────────────────────────────╯
  ? for shortcuts
"""
IDLE_NEW = """
● 完成了。
────────────────────────────────────────
❯ 
────────────────────────────────────────
  ? for shortcuts
"""
BUSY = """
● 正在讀檔案…
✻ Thinking… (12s · ↑ 1.2k tokens · esc to interrupt)
╭──────────────────────────────────────╮
│ >                                    │
╰──────────────────────────────────────╯
"""
STUCK = """
╭──────────────────────────────────────╮
│ > 幫我整理 related work               │
╰──────────────────────────────────────╯
"""
STUCK_PASTE = """
│ > [Pasted text #1 +12 lines]          │
"""


class TestScreen(unittest.TestCase):
    """畫面判讀規則。"""

    def test_states(self):
        """各種畫面要判斷成正確的狀態。"""
        self.assertEqual(screen.detect_state(TRUST_SCREEN), screen.TRUST)
        self.assertEqual(screen.detect_state(TRUST_SCREEN_NEW), screen.TRUST)
        self.assertEqual(screen.detect_state(PERMISSION_SCREEN), screen.PERMISSION)
        self.assertEqual(screen.detect_state(IDLE_BOX), screen.IDLE)
        self.assertEqual(screen.detect_state(IDLE_NEW), screen.IDLE)
        self.assertEqual(screen.detect_state(BUSY), screen.BUSY)
        self.assertEqual(screen.detect_state(""), screen.UNKNOWN)
        self.assertEqual(screen.detect_state("Please run /login to continue"), screen.LOGIN)

    def test_old_keywords_in_history_ignored(self):
        """很久以前的輸出含有關鍵字，不應影響判斷（只看最後幾行）。"""
        old = PERMISSION_SCREEN + "\n".join(f"輸出第 {i} 行" for i in range(50)) + IDLE_BOX
        self.assertEqual(screen.detect_state(old), screen.IDLE)

    def test_input_box(self):
        """輸入框內容判讀，以及「文字還卡在輸入框」的偵測。"""
        self.assertEqual(screen.input_box_line(IDLE_BOX), "")
        self.assertEqual(screen.input_box_line(STUCK), "幫我整理 related work")
        self.assertIsNone(screen.input_box_line("沒有輸入框"))
        self.assertTrue(screen.text_still_in_input(STUCK, "幫我整理 related work\n第二行"))
        self.assertTrue(screen.text_still_in_input(STUCK_PASTE, "很長的文字"))
        self.assertFalse(screen.text_still_in_input(IDLE_BOX, "幫我整理 related work"))
        self.assertFalse(screen.text_still_in_input('│ > Try "fix lint errors"', "幫我整理"))

    def test_menu_is_not_input(self):
        """「❯ 1. Yes」是選單，不能被當成輸入框。"""
        self.assertIsNone(screen.input_box_line("❯ 1. Yes\n  2. No"))

    def test_shell(self):
        """前景程式是 shell 代表 claude 已經結束。"""
        self.assertTrue(screen.is_shell("zsh"))
        self.assertFalse(screen.is_shell("claude"))


@unittest.skipUnless(shutil.which("tmux"), "沒有安裝 tmux")
class TestCli(unittest.IsolatedAsyncioTestCase):
    """用假 claude 測試 cli.py。"""

    @classmethod
    def setUpClass(cls):
        """獨立的 tmux socket。"""
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = dict(os.environ)
        os.environ["TMUX_TMPDIR"] = cls.tmp.name
        os.environ.pop("TMUX", None)
        tmux_ops.configure("tmux")
        cls.old_delay, cli.VERIFY_DELAY = cli.VERIFY_DELAY, 0.4

    @classmethod
    def tearDownClass(cls):
        """關掉 server、還原環境。"""
        asyncio.run(tmux_ops._runner.run(["kill-server"]))
        os.environ.clear()
        os.environ.update(cls.saved)
        tmux_ops.configure("tmux")
        cli.VERIFY_DELAY = cls.old_delay
        cls.tmp.cleanup()

    async def asyncTearDown(self):
        """每個測試後關掉所有 session。"""
        for s in await tmux_ops.list_sessions():
            await tmux_ops.kill_session(s.name)

    async def start(self, name, trust=False):
        """開一個假 claude。"""
        env = "FAKE_TRUST=1 " if trust else ""
        await tmux_ops.new_session(name, self.tmp.name, f"{env}{sys.executable} -u {FAKE}")

    async def test_startup_trust_then_deliver(self):
        """啟動時偵測信任提示 → 同意 → 輸入框就緒 → 送字成功。"""
        await self.start("信任測試", trust=True)
        state, _ = await cli.wait_for_startup("信任測試", 10)
        self.assertEqual(state, screen.TRUST)
        with self.assertRaises(tmux_ops.TmuxError):
            await cli.deliver("信任測試", "不應該送出")      # 信任畫面時拒絕送字
        await cli.accept_trust("信任測試")
        state, _ = await cli.wait_for_startup("信任測試", 10)
        self.assertEqual(state, screen.IDLE)
        self.assertEqual(await cli.deliver("信任測試", "你好，中文測試"), "")
        self.assertIn("● 收到：你好，中文測試", await tmux_ops.capture("信任測試"))

    async def test_permission_blocks_typing(self):
        """權限選單出現時，送字要被拒絕，避免文字被當成選單按鍵。"""
        await self.start("權限")
        await cli.wait_for_startup("權限", 10)
        await cli.deliver("權限", "請執行 ASKPERM")
        state, text = await cli.get_state("權限")
        self.assertEqual(state, screen.PERMISSION)
        # 回歸測試：送字後的「補按 Enter」絕對不能誤答權限選單（Enter＝允許）
        self.assertNotIn("權限回答", text)
        with self.assertRaises(tmux_ops.TmuxError):
            await cli.deliver("權限", "這段不能送")
        with self.assertRaises(tmux_ops.TmuxError):
            await cli.accept_trust("權限")          # 不是信任畫面就不按

    async def test_dead_session(self):
        """claude 指令找不到、馬上結束時，要回報清楚的錯誤而不是一直等。"""
        await tmux_ops.new_session("馬上結束", self.tmp.name, "true")
        with self.assertRaises(tmux_ops.TmuxError):
            await cli.wait_for_startup("馬上結束", 5)


if __name__ == "__main__":
    unittest.main()
