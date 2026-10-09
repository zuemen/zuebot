"""
端到端流程測試：真的 tmux ＋ 假 claude（fake_claude.py）＋ 假大腦（fake_claude_p.py）＋ 假 Telegram。

對照 PROMPT.md 第 10 節的驗收項目：
  - 口語問狀態 → 大腦先讀畫面再摘要（不是貼原始畫面）
  - 原話轉貼：大腦逐字複製 → 直接送；大腦改寫 → 先問你
  - 名稱有歧義 → 列出候選按鈕
  - 危險字眼（git push --force）→ 先問你，按同意才送
  - 「貼上下一則訊息」→ 多行內容原封不動、只送一次
  - 大腦失敗 → 一句人話＋提示斜線指令
  - 非白名單 → 只回「未授權」
  - 大腦拿不到任何工具（--tools ""）、hook 會略過大腦
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from zuebot import cli, tmux_ops  # noqa: E402
    from zuebot.bot import ZueBot  # noqa: E402
    from zuebot.config import Config  # noqa: E402
    from zuebot.state import State  # noqa: E402
except ImportError:
    ZueBot = None

HERE = Path(__file__).resolve().parent
USER = 111


class FakeTelegram:
    """假的 Telegram bot：記下所有送出的訊息與按鈕。"""

    def __init__(self):
        """建立空的紀錄。"""
        self.sent = []
        self.edits = []

    async def send_message(self, chat_id, text, reply_markup=None):
        """記下訊息，回傳有 message_id 的物件。"""
        msg = SimpleNamespace(chat_id=chat_id, text=text, markup=reply_markup, message_id=len(self.sent) + 1)
        self.sent.append(msg)
        return msg

    async def edit_message_text(self, chat_id=None, message_id=None, text=None):
        """記下編輯。"""
        self.edits.append(text)

    async def send_chat_action(self, chat_id, action):
        """「輸入中」狀態，不需要記。"""

    async def set_my_commands(self, commands):
        """設定指令選單，不需要記。"""

    def texts(self):
        """所有送出過的文字。"""
        return [m.text for m in self.sent]


class FakeQuery:
    """假的按鈕點擊。"""

    def __init__(self, data):
        """data 是按鈕的 callback_data。"""
        self.data = data

    async def answer(self, text=None):
        """回應點擊。"""

    async def edit_message_text(self, text):
        """按完後的訊息更新。"""


@unittest.skipIf(ZueBot is None or not shutil.which("tmux"), "需要 python-telegram-bot 與 tmux")
class TestBotFlow(unittest.IsolatedAsyncioTestCase):
    """模擬你在 Telegram 上的操作。"""

    @classmethod
    def setUpClass(cls):
        """獨立的 tmux socket，加快送字檢查。"""
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = dict(os.environ)
        os.environ["TMUX_TMPDIR"] = cls.tmp.name
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        tmux_ops.configure("tmux")
        cls.old_delay, cli.VERIFY_DELAY = cli.VERIFY_DELAY, 0.3

    @classmethod
    def tearDownClass(cls):
        """關掉 tmux server、還原環境。"""
        asyncio.run(tmux_ops._runner.run(["kill-server"]))
        os.environ.clear()
        os.environ.update(cls.saved)
        tmux_ops.configure("tmux")
        cli.VERIFY_DELAY = cls.old_delay
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        """每個測試：新的資料夾、假大腦劇本、假 Telegram。"""
        self.dir = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.script = self.dir / "script.json"
        self.brain_log = self.dir / "brain.log"
        os.environ["FAKE_BRAIN_SCRIPT"] = str(self.script)
        os.environ["FAKE_BRAIN_LOG"] = str(self.brain_log)
        cfg = Config(bot_token="1:x", allowed_user_ids=frozenset({USER}), allowed_root=self.dir,
                     claude_cmd=f"{sys.executable} -u {HERE / 'fake_claude.py'}", tmux_bin="tmux",
                     data_dir=self.dir, events_file=self.dir / "events.jsonl", state_file=self.dir / "state.json",
                     brain_cmd=str(HERE / "fake_claude_p.py"), confirm_timeout=5)
        self.cfg = cfg
        self.zb = ZueBot(cfg, State(cfg.state_file))
        self.tg = FakeTelegram()
        self.zb.app = SimpleNamespace(bot=self.tg)

    async def asyncTearDown(self):
        """關掉所有 session。"""
        for s in await tmux_ops.list_sessions():
            await tmux_ops.kill_session(s.name)

    def brain_says(self, *responses):
        """設定假大腦接下來的回應（依序）。"""
        self.script.write_text(json.dumps(list(responses), ensure_ascii=False), encoding="utf-8")

    def brain_calls(self):
        """讀出假大腦被呼叫的紀錄。"""
        if not self.brain_log.exists():
            return []
        return [json.loads(line) for line in self.brain_log.read_text(encoding="utf-8").splitlines()]

    async def open_cli(self, name):
        """開一個假 claude 並等它就緒。"""
        await tmux_ops.new_session(name, str(self.dir), self.cfg.claude_cmd)
        await cli.wait_for_startup(name, 10)

    async def say(self, text, user=USER):
        """模擬你傳一則訊息。"""
        msg = SimpleNamespace(text=text, replies=[])

        async def reply_text(t):
            msg.replies.append(t)
        msg.reply_text = reply_text
        update = SimpleNamespace(effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=user),
                                 effective_message=msg, callback_query=None)
        ctx = SimpleNamespace(args=text.split()[1:])
        if text.startswith("/send "):
            await self.zb.cmd_send(update, ctx)
        else:
            await self.zb.on_text(update, ctx)
        return msg

    async def press(self, label):
        """按下最近一則訊息上、文字包含 label 的按鈕。"""
        for m in reversed(self.tg.sent):
            if m.markup:
                for row in m.markup.inline_keyboard:
                    for btn in row:
                        if label in btn.text:
                            update = SimpleNamespace(effective_user=SimpleNamespace(id=USER),
                                                     callback_query=FakeQuery(btn.callback_data), effective_message=None)
                            await self.zb.on_button(update, None)
                            return
        self.fail(f"找不到按鈕「{label}」，訊息：{self.tg.texts()}")

    async def screen(self, name):
        """讀畫面。"""
        return await tmux_ops.capture(name, 60)

    async def test_status_question_reads_screen_then_summarizes(self):
        """「競賽那個在幹嘛」→ 大腦先 read_screen，第二輪拿到畫面後給摘要。"""
        await self.open_cli("競賽")
        self.brain_says(
            {"reply": "", "done": False, "actions": [{"tool": "read_screen", "name": "競賽"}]},
            {"reply": "競賽那個目前閒置，正在等你輸入。", "done": True, "actions": []})
        await self.say("競賽那個在幹嘛？")
        calls = self.brain_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("Welcome to Fake Claude", calls[1]["prompt"])      # 第二輪有看到畫面
        self.assertIn("競賽那個目前閒置，正在等你輸入。", self.tg.texts())
        self.assertFalse(any("Welcome to Fake Claude" in t for t in self.tg.texts()))   # 沒有貼原始畫面給你

    async def test_brain_has_no_tools_and_skips_hook(self):
        """大腦呼叫時：--tools "" 關掉所有工具、不載入 MCP、標記 ZUEBOT_BRAIN、沒有 TMUX_PANE。"""
        self.brain_says({"reply": "你好", "done": True, "actions": []})
        await self.say("你好")
        call = self.brain_calls()[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "mcp__*")   # 外掛的 MCP 工具也禁止
        self.assertTrue(argv[argv.index("--disallowedTools") + 2].startswith("--"))  # 後面緊接選項，不會吃掉其他參數
        self.assertIn("--safe-mode", argv)                                      # 支援時會加上 safe mode
        self.assertEqual(call["ZUEBOT_BRAIN"], "1")
        self.assertIsNone(call["TMUX_PANE"])

    async def test_verbatim_relay(self):
        """原話：逐字複製 → 直接送；大腦改寫過 → 先問你，不會直接送。"""
        await self.open_cli("競賽")
        self.brain_says({"reply": "好，已轉告。", "done": True,
                         "actions": [{"tool": "send_text", "name": "競賽", "text": "改用 v2 資料集"}]})
        await self.say("跟競賽那個說改用 v2 資料集")
        await asyncio.sleep(0.5)
        self.assertIn("收到：改用 v2 資料集", await self.screen("競賽"))
        self.assertEqual(self.zb.state.watcher("競賽"), USER)              # 自動關注

        self.brain_says({"reply": "好。", "done": True,
                         "actions": [{"tool": "send_text", "name": "競賽", "text": "請你改用第三版的資料集"}]})
        await self.say("跟競賽那個說換成 v3")
        await asyncio.sleep(0.5)
        self.assertNotIn("第三版", await self.screen("競賽"))
        self.assertTrue(any("不是你的原話" in t for t in self.tg.texts()))

    async def test_ambiguous_name_lists_candidates(self):
        """兩個名稱相近時，列出候選讓你選，選了才執行。"""
        await self.open_cli("demo-a")
        await self.open_cli("demo-b")
        self.brain_says({"reply": "", "done": True, "actions": [{"tool": "watch", "name": "demo"}]})
        await self.say("關注 demo 那個")
        self.assertTrue(any("符合好幾個" in t for t in self.tg.texts()))
        self.assertIsNone(self.zb.state.watcher("demo-a"))
        await self.press("demo-b")
        self.assertEqual(self.zb.state.watcher("demo-b"), USER)
        self.assertIsNone(self.zb.state.watcher("demo-a"))

    async def test_dangerous_text_needs_confirmation(self):
        """「git push --force」要先確認；按同意才送出。"""
        await self.open_cli("競賽")
        self.brain_says({"reply": "", "done": True,
                         "actions": [{"tool": "send_text", "name": "競賽", "text": "執行 git push --force"}]})
        await self.say("幫我在競賽那個執行 git push --force")
        await asyncio.sleep(0.5)
        self.assertNotIn("git push", await self.screen("競賽"))
        self.assertTrue(any("需要你確認" in t and "git push" in t for t in self.tg.texts()))
        await self.press("同意")
        await asyncio.sleep(0.5)
        self.assertIn("收到：執行 git push --force", await self.screen("競賽"))

    async def test_send_key_needs_confirmation_and_cancel_works(self):
        """送按鍵一律先問；按取消就不送。"""
        await self.open_cli("競賽")
        self.brain_says({"reply": "", "done": True, "actions": [{"tool": "send_key", "name": "競賽", "key": "y"}]})
        await self.say("幫競賽按 y")
        await self.press("取消")
        await tmux_ops.send_key("競賽", "enter")
        await asyncio.sleep(0.4)
        self.assertNotIn("收到：y", await self.screen("競賽"))

    async def test_paste_next_message_multiline(self):
        """「貼上接下來這段」→ 下一則多行訊息原封不動送進去（假 CLI 會逐行收到），而且不經過大腦。"""
        await self.open_cli("競賽")
        self.brain_says({"reply": "", "done": True, "actions": [{"tool": "arm_paste", "name": "競賽"}]})
        await self.say("幫我貼上接下來這段訊息到競賽")
        await self.say("第一行\n第二行 rm 這個字只是文字")
        # 含有 rm → 仍然要確認（安全規則優先）
        await self.press("同意")
        await asyncio.sleep(0.6)
        text = await self.screen("競賽")
        self.assertIn("收到：第一行", text)
        self.assertEqual(len(self.brain_calls()), 1)                         # 第二則沒有交給大腦

    async def test_direct_prefix(self):
        """以 > 開頭：原文直接送進目前對象。"""
        await self.open_cli("競賽")
        self.zb.state.set_current(USER, "競賽")
        await self.say("> 繼續做下一步")
        await asyncio.sleep(0.5)
        self.assertIn("收到：繼續做下一步", await self.screen("競賽"))
        self.assertEqual(self.brain_calls(), [])

    async def test_brain_failure_is_human(self):
        """大腦失敗（沒登入）→ 一句人話＋斜線指令提示。"""
        self.brain_says("__FAIL__")
        await self.say("現在有哪些 CLI？")
        last = self.tg.texts()[-1]
        self.assertIn("大腦暫時沒辦法處理", last)
        self.assertIn("/list", last)

    async def test_unauthorized(self):
        """非白名單：只回「未授權」與對方 id，不呼叫大腦。"""
        msg = await self.say("現在有哪些 CLI？", user=999)
        self.assertEqual(msg.replies, ["未授權。你的 user id 是 999"])
        self.assertEqual(self.brain_calls(), [])

    async def fire_stop(self, name, reply):
        """模擬 hook：替某個 CLI 寫一個 Stop 事件，然後讓監控處理。"""
        pane = next(s.pane_id for s in await tmux_ops.list_sessions() if s.name == name)
        with self.cfg.events_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "Stop", "pane": pane, "reply": reply}, ensure_ascii=False) + "\n")
        before = len(self.tg.sent)
        await self.zb.monitor.tick()
        for _ in range(50):                      # 完成回報在背景執行，最多等 5 秒
            if len(self.tg.sent) > before:
                break
            await asyncio.sleep(0.1)

    async def test_completion_report(self):
        """完成回報：長回覆 → 大腦摘要＋「原文」按鈕；短回覆直接轉；沒關注的不通知。"""
        await self.open_cli("競賽")
        self.zb.monitor.reader.read_new()             # 從檔尾開始
        self.zb.state.watch("競賽", USER)
        self.brain_says("競賽那個把模型訓練完了，準確率 91%，沒有錯誤。")
        await self.fire_stop("競賽", "訓練紀錄：" + "epoch 進度……" * 100)
        self.assertTrue(any(t.startswith("✅ [競賽] 這輪完成") and "準確率 91%" in t for t in self.tg.texts()))
        await self.press("原文")
        self.assertTrue(any(t.startswith("📄 完整回覆") and "訓練紀錄" in t for t in self.tg.texts()))

        await self.fire_stop("競賽", "好了，hello.py 建立完成。")
        self.assertIn("✅ [競賽] 這輪完成：\n\n好了，hello.py 建立完成。", self.tg.texts())
        self.assertEqual(len(self.brain_calls()), 1)  # 短回覆不用大腦

        self.zb.state.unwatch("競賽")
        before = len(self.tg.sent)
        await self.fire_stop("競賽", "這則不該通知")
        self.assertEqual(len(self.tg.sent), before)

    async def wait_for_text(self, needle, seconds=8):
        """等待某段文字出現在送出的訊息裡。"""
        for _ in range(int(seconds * 10)):
            if any(needle in m for m in self.tg.texts()):
                return
            await asyncio.sleep(0.1)
        self.fail(f"等不到「{needle}」，訊息：{self.tg.texts()}")

    async def test_permission_buttons(self):
        """權限確認：🔔 通知＋依畫面選項產生的按鈕；按了才送鍵；畫面變了就不送。"""
        await self.open_cli("競賽")
        self.zb.monitor.reader.read_new()
        self.zb.state.watch("競賽", USER)
        await tmux_ops.paste_text("競賽", "請執行 ASKPERM")
        await asyncio.sleep(0.5)
        pane = next(s.pane_id for s in await tmux_ops.list_sessions() if s.name == "競賽")
        with self.cfg.events_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "PermissionRequest", "pane": pane, "tool_name": "Bash",
                                "tool_input": {"command": "echo hi"}}) + "\n")
            f.write(json.dumps({"event": "Notification", "pane": pane, "notification_type": "permission_prompt",
                                "message": "Claude needs your permission"}) + "\n")
        await self.zb.monitor.tick()
        await self.wait_for_text("🔔 [競賽] 在等你確認")
        await asyncio.sleep(1.5)       # 等 Notification 那一則也處理完
        self.assertEqual(sum("🔔 [競賽] 在等你確認" in m for m in self.tg.texts()), 1)   # 同一個畫面只通知一次
        self.assertTrue(any("它想使用 Bash" in m for m in self.tg.texts()))
        self.assertNotIn("權限回答", await self.screen("競賽"))       # 還沒按，不會送
        await self.press("1. Yes")
        await asyncio.sleep(0.5)
        self.assertIn("權限回答：'1'", await self.screen("競賽"))
        # 同一則通知上的另一個按鈕已失效；就算畫面變了也不會亂送
        result = await self.zb.monitor.answer_permission("競賽", USER, "esc", "拒絕")
        self.assertIn("已經不是權限確認畫面", result)

    async def test_session_closed_manually(self):
        """關注中的 session 被手動關掉 → 自動從關注清單移除，只通知一次。"""
        await self.open_cli("競賽")
        self.zb.state.watch("競賽", USER)
        await tmux_ops.kill_session("競賽")
        await self.zb.monitor.check_sessions()
        await self.zb.monitor.check_sessions()
        self.assertIsNone(self.zb.state.watcher("競賽"))
        self.assertEqual(sum("已經被關掉了" in m for m in self.tg.texts()), 1)

    async def test_fallback_idle_without_hook(self):
        """沒有 hook 事件時：畫面連續沒變且在等輸入 → 視為完成並回報（附上說明）。"""
        from zuebot import monitor as monitor_mod
        await self.open_cli("競賽")
        self.zb.state.watch("競賽", USER)
        self.zb.toolbox.awaiting["競賽"] = 0          # 很久以前送出的訊息，一直沒收到 Stop
        self.brain_says("競賽那個看起來已經做完，正在等你下一步。")
        for _ in range(monitor_mod.FALLBACK_SAME_TIMES + 1):
            await self.zb.monitor.fallback_idle()
        await self.wait_for_text("沒有收到 hook 通知")
        self.assertNotIn("競賽", self.zb.toolbox.awaiting)

    async def test_paste_timeout_reminder(self):
        """「貼上下一則」逾時 → 通知你一次，之後的訊息不會被貼上。"""
        from zuebot import tools as tools_mod
        await self.open_cli("競賽")
        old, tools_mod.PASTE_TIMEOUT = tools_mod.PASTE_TIMEOUT, 0.3
        try:
            await self.zb.toolbox.arm_paste("競賽", USER)
            await self.wait_for_text("已經過了 5 分鐘")
            self.assertEqual(self.zb.toolbox.take_paste(USER), (None, False))
        finally:
            tools_mod.PASTE_TIMEOUT = old

    async def test_restart_keeps_watches(self):
        """重開 bot：關注清單與目前對象都還在，啟動通知會列出關注中的 CLI。"""
        self.zb.state.watch("競賽", USER)
        self.zb.state.set_current(USER, "競賽")
        restarted = ZueBot(self.cfg, State.load(self.cfg.state_file))
        tg = FakeTelegram()
        restarted.app = SimpleNamespace(bot=tg)
        await restarted.post_init(SimpleNamespace(bot=tg))
        await restarted.post_shutdown(None)
        self.assertEqual(restarted.state.watcher("競賽"), USER)
        self.assertEqual(restarted.state.get_current(USER), "競賽")
        self.assertTrue(any("zuebot 已啟動" in m and "競賽" in m for m in tg.texts()))

    async def test_double_tap_runs_once(self):
        """確認按鈕連點兩下（bot 同時處理兩個點擊）只會執行一次。"""
        await self.open_cli("競賽")
        await self.say("/send 競賽 git push --force 只送一次")
        btn = next(b for m in reversed(self.tg.sent) if m.markup for row in m.markup.inline_keyboard
                   for b in row if "同意" in b.text)
        tap = SimpleNamespace(effective_user=SimpleNamespace(id=USER), callback_query=FakeQuery(btn.callback_data),
                              effective_message=None)
        await asyncio.gather(self.zb.on_button(tap, None), self.zb.on_button(tap, None))
        await asyncio.sleep(0.6)
        self.assertEqual((await self.screen("競賽")).count("收到：git push --force 只送一次"), 1)

    async def test_old_permission_button_refuses_new_prompt(self):
        """舊的權限按鈕：畫面換成另一個確認時拒絕送鍵。"""
        await self.open_cli("競賽")
        await tmux_ops.paste_text("競賽", "請執行 ASKPERM")
        await asyncio.sleep(0.5)
        result = await self.zb.monitor.answer_permission("競賽", USER, "1", "Yes", signature="另一個確認畫面的指紋")
        self.assertIn("不一樣了", result)
        self.assertNotIn("權限回答", await self.screen("競賽"))

    async def test_plain_terminal(self):
        """一般終端機：清單標示出來；送指令一律要確認；指令跑完回到提示字元時回報。"""
        from zuebot import monitor as monitor_mod
        await tmux_ops.new_session("term", str(self.dir), "sh")
        await asyncio.sleep(0.5)
        listing = await self.zb.toolbox.list_clis(USER)
        self.assertIn("一般終端機", listing.data[0]["state"])
        result = await self.zb.toolbox.send_text("term", "echo zuebot-ok", USER)
        self.assertIsNotNone(result.pending)                       # 一定要確認
        self.assertIn("當成指令直接執行", result.pending.title)
        self.assertNotIn("zuebot-ok\n", await self.screen("term"))
        await self.zb.present(USER, await result.pending.run())   # 模擬按同意
        await asyncio.sleep(0.5)
        self.assertIn("zuebot-ok", await self.screen("term"))
        self.zb.toolbox.awaiting["term"] = 0
        self.brain_says("終端機印出了 zuebot-ok，沒有錯誤。")
        for _ in range(monitor_mod.FALLBACK_SAME_TIMES + 1):
            await self.zb.monitor.fallback_idle()
        await self.wait_for_text("回到提示字元")

    async def test_new_cli_outside_root_rejected(self):
        """new_cli 只能在 ALLOWED_ROOT 底下。"""
        self.brain_says({"reply": "", "done": True,
                         "actions": [{"tool": "new_cli", "name": "x", "cwd": "/etc", "first_prompt": ""}]})
        await self.say("在 /etc 開一個 CLI")
        self.assertTrue(any("只能在" in t for t in self.tg.texts()))
        self.assertFalse(await tmux_ops.session_exists("x"))


if __name__ == "__main__":
    unittest.main()
