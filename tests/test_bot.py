"""
測試 bot.py 中不需要連上 Telegram 的部分：訊息切段、白名單、Application 建立。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from zuebot import bot  # noqa: E402
    from zuebot.config import Config  # noqa: E402
    from zuebot.state import State  # noqa: E402
except ImportError:          # 沒裝 python-telegram-bot 時略過
    bot = None


def make_config(tmp: str) -> "Config":
    """建立測試用設定（假的 token，不會真的連線）。"""
    d = Path(tmp)
    return Config(bot_token="123456:TEST", allowed_user_ids=frozenset({111}), allowed_root=d,
                  claude_cmd="claude", tmux_bin="tmux", data_dir=d,
                  events_file=d / "events.jsonl", state_file=d / "state.json")


class FakeMessage:
    """假的 Telegram 訊息：記下 bot 回覆了什麼。"""

    def __init__(self, text=""):
        """text 是使用者傳來的文字。"""
        self.text = text
        self.replies = []

    async def reply_text(self, text):
        """記下回覆內容。"""
        self.replies.append(text)


@unittest.skipIf(bot is None, "沒有安裝 python-telegram-bot")
class TestBot(unittest.IsolatedAsyncioTestCase):
    """bot 的離線測試。"""

    def test_split_message(self):
        """長訊息要切段，每段都不超過上限，而且內容完整。"""
        text = "\n".join(f"第 {i} 行" for i in range(2000))
        chunks = bot.split_message(text, 500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("\n".join(chunks), text)
        self.assertEqual(bot.split_message(""), ["（空白）"])
        self.assertEqual("".join(bot.split_message("x" * 1200, 500)), "x" * 1200)

    async def test_unauthorized_gets_no_info(self):
        """非白名單使用者只會收到「未授權」和自己的 user id。"""
        with tempfile.TemporaryDirectory() as d:
            zb = bot.ZueBot(make_config(d), State(Path(d) / "state.json"))
            msg = FakeMessage("/list")
            update = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=msg,
                                     effective_chat=SimpleNamespace(id=999))
            await zb.cmd_list(update, SimpleNamespace(args=[]))
            self.assertEqual(msg.replies, ["未授權。你的 user id 是 999"])

    def test_build_application(self):
        """所有指令都能註冊成功（不連線）。"""
        with tempfile.TemporaryDirectory() as d:
            zb = bot.ZueBot(make_config(d), State(Path(d) / "state.json"))
            app = zb.build_application()
            commands = {c for h in app.handlers[0] if hasattr(h, "commands") for c in h.commands}
            self.assertTrue({"list", "use", "look", "send", "paste", "watch", "unwatch",
                             "key", "new", "kill", "start", "help"} <= commands)
            self.assertIn("/list", bot.HELP)


if __name__ == "__main__":
    unittest.main()
