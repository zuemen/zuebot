"""
bot.py —— Telegram 指令處理（Phase 0：搬自 v0 的斜線指令版本）

能做的事：
  /list                       列出所有 CLI（tmux session）
  /use 競賽                   設定「目前對象」，之後直接打字就會送進去
  （直接打字）                 送進目前對象的 CLI，並自動關注，完成時回報
  /look [競賽] [行數]          看 CLI 目前的畫面
  /send 競賽 訊息              不切換對象，直接送一段訊息
  /paste [競賽]                下一則訊息（可多行）原封不動貼進去（5 分鐘內有效）
  /watch 競賽　/unwatch 競賽   關注／取消關注（完成或需要你確認時通知）
  /key 競賽 1                  送按鍵：1 2 3 y n enter esc up down tab ctrl-c
  /new 名稱 資料夾 [第一個任務] 開新的 tmux + claude
  /kill 名稱 yes              關閉某個 CLI（要加 yes 才會執行）

之後的 Phase 會再加上：自然語言大腦、確認按鈕、危險字眼偵測、機密遮蔽等。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import cli, screen, tmux_ops
from .config import Config
from .events import EventReader
from .state import State
from .tmux_ops import TmuxError

log = logging.getLogger(__name__)

MAX_TG = 3900               # Telegram 單則上限 4096 字，留一點餘裕
PASTE_TIMEOUT = 300         # /paste 等待下一則訊息的秒數
EVENT_POLL_SECONDS = 2      # 每幾秒讀一次事件檔

# /help 顯示的說明：直接取用本檔開頭說明中「能做的事」那一段，兩邊永遠一致
HELP = "能做的事：\n" + (__doc__ or "").split("能做的事：")[1].split("之後的 Phase")[0].strip()

# 顯示在 Telegram 輸入框「/」選單裡的指令說明
COMMANDS = [
    ("list", "列出所有 CLI"), ("use", "設定目前對象"), ("look", "看畫面"), ("send", "送一段訊息"),
    ("paste", "下一則訊息原封不動貼進去"), ("watch", "關注"), ("unwatch", "取消關注"),
    ("key", "送按鍵"), ("new", "開新的 CLI"), ("kill", "關閉 CLI"), ("help", "說明"),
]

Handler = Callable[["ZueBot", Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def authorized_only(func: Handler) -> Handler:
    """
    裝飾器：只有白名單內的使用者才能執行這個指令。
    其他人只會收到「未授權」和他自己的 user id，不洩漏任何系統資訊（PROMPT.md 第 6 節第 1 點）。
    """
    @functools.wraps(func)
    async def wrapper(self: "ZueBot", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in self.cfg.allowed_user_ids:
            if update.effective_message:
                await update.effective_message.reply_text(f"未授權。你的 user id 是 {user.id if user else '未知'}")
            log.info("拒絕未授權使用者 %s", user.id if user else "未知")
            return
        await func(self, update, ctx)
    return wrapper


def split_message(text: str, limit: int = MAX_TG) -> list[str]:
    """
    把長文字切成 Telegram 能傳的段落。盡量在換行處切，不要把一行切成兩半。
    """
    text = text or "（空白）"
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit       # 整段沒有換行，只好硬切
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


class ZueBot:
    """把設定、狀態和所有 Telegram 指令包在一起的主類別。"""

    def __init__(self, cfg: Config, state: State) -> None:
        """準備好設定、狀態和事件讀取器。"""
        self.cfg = cfg
        self.state = state
        self.reader = EventReader(cfg.events_file, state.events_offset)
        self.paste_armed: dict[int, tuple[str, float]] = {}   # chat_id → (session, 到期時間)；不存檔
        self._event_task: asyncio.Task | None = None

    # ───────────── 小工具 ─────────────
    async def send(self, app: Application, chat_id: int, text: str) -> None:
        """傳訊息到 Telegram，太長就自動切段。用純文字，不用 Markdown，終端畫面裡的符號才不會出錯。"""
        for chunk in split_message(text):
            await app.bot.send_message(chat_id=chat_id, text=chunk)

    async def reply(self, update: Update, text: str) -> None:
        """回覆使用者剛才那則訊息（太長自動切段）。"""
        for chunk in split_message(text):
            await update.effective_message.reply_text(chunk)

    async def need_session(self, update: Update, name: str | None) -> str | None:
        """確認 session 存在；不存在就回覆錯誤並列出現有的，回傳 None。"""
        if name and await tmux_ops.session_exists(name):
            return name
        names = "、".join(s.name for s in await tmux_ops.list_sessions()) or "（目前沒有）"
        if name:
            await self.reply(update, f"找不到 [{name}]。現有：{names}")
        else:
            await self.reply(update, f"請指定 CLI 名稱，或先用 /use 設定目前對象。現有：{names}")
        return None

    async def deliver(self, update: Update, name: str, text: str) -> None:
        """送訊息進 CLI，並自動關注，完成時會回報。"""
        try:
            note = await cli.deliver(name, text)
        except TmuxError as e:
            await self.reply(update, f"❌ {e}")
            return
        self.state.watch(name, update.effective_chat.id)
        await self.reply(update, f"📨 已送到 [{name}]，完成時會通知你。{note}")

    # ───────────── Telegram 指令 ─────────────
    @authorized_only
    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/start、/help：顯示說明。"""
        await self.reply(update, HELP)

    @authorized_only
    async def cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/list：列出所有 CLI、工作目錄、前景程式，並標出目前對象和關注中的。"""
        sessions = await tmux_ops.list_sessions()
        if not sessions:
            await self.reply(update, "目前沒有 CLI 在跑。用 /new 開一個，或在電腦上用 cc 腳本開。")
            return
        cur = self.state.get_current(update.effective_chat.id)
        lines = []
        for s in sessions:
            mark = "👉" if s.name == cur else "▫️"
            eye = " 👀" if self.state.watcher(s.name) else ""
            lines.append(f"{mark} {s.name}{eye}  [{s.command}]\n     {s.path}")
        await self.reply(update, "\n".join(lines) + "\n\n👉 目前對象　👀 關注中")

    @authorized_only
    async def cmd_use(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/use 名稱：設定目前對象。"""
        name = await self.need_session(update, ctx.args[0] if ctx.args else None)
        if not name:
            return
        self.state.set_current(update.effective_chat.id, name)
        await self.reply(update, f"👉 目前對象：{name}。之後直接打字就會送進去。")

    @authorized_only
    async def cmd_look(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/look [名稱] [行數]：看畫面原文。只給數字時當作行數，對象用目前對象。"""
        args = list(ctx.args)
        lines = 40
        if args and args[-1].isdigit():
            lines = int(args.pop())
        name = args[0] if args else self.state.get_current(update.effective_chat.id)
        name = await self.need_session(update, name)
        if not name:
            return
        try:
            screen = await tmux_ops.capture(name, lines)
        except TmuxError as e:
            await self.reply(update, f"❌ {e}")
            return
        await self.reply(update, f"🖥 [{name}] 目前畫面：\n\n{screen}")

    @authorized_only
    async def cmd_send(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/send 名稱 訊息：直接送一段訊息，不改變目前對象。"""
        parts = (update.effective_message.text or "").split(maxsplit=2)
        if len(parts) < 3:
            await self.reply(update, "用法：/send <名稱> <訊息>")
            return
        name = await self.need_session(update, parts[1])
        if name:
            await self.deliver(update, name, parts[2])

    @authorized_only
    async def cmd_paste(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/paste [名稱]：下一則訊息會原封不動貼進去。"""
        name = ctx.args[0] if ctx.args else self.state.get_current(update.effective_chat.id)
        name = await self.need_session(update, name)
        if not name:
            return
        self.paste_armed[update.effective_chat.id] = (name, time.time() + PASTE_TIMEOUT)
        await self.reply(update, f"📋 好，下一則訊息會原封不動貼進 [{name}]（5 分鐘內有效）。")

    @authorized_only
    async def cmd_watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/watch 名稱：開始關注，並附上目前畫面最後幾行。"""
        name = await self.need_session(update, ctx.args[0] if ctx.args else None)
        if not name:
            return
        self.state.watch(name, update.effective_chat.id)
        try:
            screen = await tmux_ops.tail(name, 15)
        except TmuxError as e:
            screen = f"（{e}）"
        await self.reply(update, f"👀 開始關注 [{name}]，完成或需要你確認時會通知。\n目前畫面最後幾行：\n\n{screen}")

    @authorized_only
    async def cmd_unwatch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/unwatch 名稱：取消關注。"""
        name = ctx.args[0] if ctx.args else ""
        if not self.state.unwatch(name):
            await self.reply(update, f"[{name}] 本來就沒有在關注")
            return
        await self.reply(update, f"🙈 已停止關注 [{name}]")

    @authorized_only
    async def cmd_key(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /key 名稱 按鍵：送一個按鍵（用於權限確認選單）。
        注意：Phase 3 會改成「先轉述畫面在問什麼、按確認按鈕才送出」。
        """
        if len(ctx.args) != 2 or ctx.args[1].lower() not in tmux_ops.KEYS:
            await self.reply(update, f"用法：/key <名稱> <{' / '.join(tmux_ops.KEYS)}>")
            return
        name = await self.need_session(update, ctx.args[0])
        if not name:
            return
        try:
            await tmux_ops.send_key(name, ctx.args[1])
            self.state.watch(name, update.effective_chat.id)
            await asyncio.sleep(1.5)                  # 等畫面更新後回傳，讓你確認按對了
            screen = await tmux_ops.tail(name, 12)
        except TmuxError as e:
            await self.reply(update, f"❌ {e}")
            return
        await self.reply(update, f"⌨️ 已對 [{name}] 按下 {ctx.args[1]}\n\n{screen}")

    def _resolve_folder(self, folder: str) -> Path | None:
        """把使用者給的資料夾轉成絕對路徑；不存在或不在 ALLOWED_ROOT 底下就回傳 None。"""
        try:
            cwd = Path(folder).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        root = self.cfg.allowed_root
        if not cwd.is_dir() or not (cwd == root or root in cwd.parents):
            return None
        return cwd

    @authorized_only
    async def cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/new 名稱 資料夾 [第一個任務]：開新的 tmux session 並啟動 claude。"""
        parts = (update.effective_message.text or "").split(maxsplit=3)
        if len(parts) < 3:
            await self.reply(update, "用法：/new <名稱> <資料夾> [第一個任務]")
            return
        name, folder = parts[1], parts[2]
        first = parts[3] if len(parts) > 3 else ""
        cwd = self._resolve_folder(folder)
        if cwd is None:
            await self.reply(update, f"資料夾不存在，或不在允許範圍 {self.cfg.allowed_root} 底下")
            return
        try:
            await tmux_ops.new_session(name, str(cwd), self.cfg.claude_cmd)
        except TmuxError as e:
            await self.reply(update, f"❌ {e}")
            return
        chat_id = update.effective_chat.id
        self.state.set_current(chat_id, name)
        self.state.watch(name, chat_id)
        await self.reply(update, f"🆕 已開 [{name}]（{cwd}），設為目前對象，等待 claude 啟動…")
        try:
            state, text = await cli.wait_for_startup(name)
        except TmuxError as e:
            await self.reply(update, f"❌ {e}")
            return
        if state == screen.IDLE:
            if first:
                await self.deliver(update, name, first)
            else:
                await self.reply(update, f"✅ [{name}] 已就緒，可以開始傳訊息了。")
            return
        hint = {
            screen.TRUST: f"第一次在這個資料夾開 claude，它在問是否信任。確認沒問題請傳 /key {name} enter，"
                          f"然後再把任務傳給它。",
            screen.LOGIN: "claude 還沒登入，請回到電腦上執行 claude 並輸入 /login。",
        }.get(state, "等了一段時間還沒看到輸入框，請看一下畫面。")
        await self.reply(update, f"⚠️ [{name}] {hint}\n\n{screen.recent(text, 15)}")

    @authorized_only
    async def cmd_kill(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/kill 名稱 yes：關閉 CLI。Phase 3 會改成確認按鈕。"""
        if len(ctx.args) != 2 or ctx.args[1] != "yes":
            await self.reply(update, "為了避免誤關，請輸入：/kill <名稱> yes")
            return
        name = await self.need_session(update, ctx.args[0])
        if not name:
            return
        try:
            await tmux_ops.kill_session(name)
        except TmuxError as e:
            await self.reply(update, f"❌ {e}")
            return
        self.state.forget_session(name)
        await self.reply(update, f"🗑 已關閉 [{name}]")

    @authorized_only
    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """一般文字：先看是不是 /paste 等待中的內容，否則送進目前對象。（Phase 2 會改成交給大腦）"""
        chat_id = update.effective_chat.id
        text = update.effective_message.text or ""
        armed = self.paste_armed.pop(chat_id, None)
        if armed and time.time() < armed[1]:
            name = await self.need_session(update, armed[0])
        else:
            if armed:
                await self.reply(update, "（/paste 已逾時，改送到目前對象）")
            name = self.state.get_current(chat_id)
            if not name:
                await self.reply(update, "還沒設定目前對象，先 /use <名稱>，或 /list 看看有哪些")
                return
            name = await self.need_session(update, name)
        if name:
            await self.deliver(update, name, text)

    # ───────────── 背景：讀 hook 事件並通知 ─────────────
    async def handle_event(self, app: Application, ev: dict[str, Any]) -> None:
        """處理一個 hook 事件：找出是哪個 CLI、有沒有人關注，再組成通知訊息。"""
        name = await tmux_ops.session_of_pane(str(ev.get("pane", "")))
        if not name:
            return                                  # 那個 pane 已經不在了
        chat_id = self.state.watcher(name)
        if not chat_id:
            return                                  # 沒在關注的 CLI 不打擾你
        kind = ev.get("event")
        if kind == "Stop":
            reply = str(ev.get("reply") or "").strip()
            if not reply:                           # 抓不到回覆文字時，改附畫面
                try:
                    reply = "（抓不到回覆文字，附上畫面）\n\n" + await tmux_ops.capture(name, 30)
                except TmuxError as e:
                    reply = f"（抓不到回覆文字，也讀不到畫面：{e}）"
            await self.send(app, chat_id, f"✅ [{name}] 這輪完成：\n\n{reply}")
        elif kind == "Notification":
            if ev.get("notification_type") == "idle_prompt":
                return   # 閒置約 60 秒的提醒：Stop 時已經通知過完成了，不重複打擾
            try:
                screen = await tmux_ops.tail(name, 15)
            except TmuxError as e:
                screen = f"（{e}）"
            await self.send(app, chat_id,
                            f"🔔 [{name}] 在等你：{ev.get('message', '')}\n\n{screen}\n\n"
                            f"可用 /key {name} 1（或 enter / esc）回應")

    async def event_loop(self, app: Application) -> None:
        """每 EVENT_POLL_SECONDS 秒讀一次事件檔，有新事件就處理。任何錯誤都只記 log，迴圈不會停。"""
        while True:
            try:
                events = self.reader.read_new()
                if self.reader.offset != self.state.events_offset:
                    self.state.events_offset = self.reader.offset
                    self.state.save()
                for ev in events:
                    log.debug("hook 事件：%s", ev)
                    try:
                        await self.handle_event(app, ev)
                    except Exception:
                        log.exception("處理事件失敗：%s", ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("事件迴圈發生錯誤")
            await asyncio.sleep(EVENT_POLL_SECONDS)

    # ───────────── 生命週期 ─────────────
    async def post_init(self, app: Application) -> None:
        """bot 連上 Telegram 後執行：設定指令選單、啟動事件迴圈。"""
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except Exception:
            log.warning("設定指令選單失敗（不影響使用）", exc_info=True)
        # 自己保存 task，關閉時才能取消它（在 post_init 用 app.create_task 不會被 PTB 追蹤）
        self._event_task = asyncio.create_task(self.event_loop(app))

    async def post_shutdown(self, app: Application) -> None:
        """bot 關閉時執行：停止事件迴圈、存檔。"""
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except (asyncio.CancelledError, Exception):
                pass
        self.state.save()

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """全域錯誤處理：記到 log。（Phase 4 會再加上「用一句人話通知你」）"""
        log.error("處理更新時發生錯誤", exc_info=ctx.error)

    def build_application(self) -> Application:
        """建立 Telegram Application 並註冊所有指令。"""
        app = (Application.builder().token(self.cfg.bot_token)
               .post_init(self.post_init).post_shutdown(self.post_shutdown).build())
        handlers = [("start", self.cmd_help), ("help", self.cmd_help), ("list", self.cmd_list),
                    ("use", self.cmd_use), ("look", self.cmd_look), ("send", self.cmd_send),
                    ("paste", self.cmd_paste), ("watch", self.cmd_watch), ("unwatch", self.cmd_unwatch),
                    ("key", self.cmd_key), ("new", self.cmd_new), ("kill", self.cmd_kill)]
        for cmd, fn in handlers:
            app.add_handler(CommandHandler(cmd, fn))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self.on_error)
        return app


def run(cfg: Config) -> None:
    """讀取狀態、建立 bot 並開始 polling（會一直執行到按 Ctrl-C）。"""
    if not cfg.allowed_user_ids:
        log.warning("⚠️ 還沒設定 ALLOWED_USER_IDS：先傳任何訊息給 bot 取得你的 user id，填進 .env 後重開。")
    state = State.load(cfg.state_file)
    bot = ZueBot(cfg, state)
    app = bot.build_application()
    log.info("Bot 啟動。允許的資料夾根目錄：%s；事件檔：%s", cfg.allowed_root, cfg.events_file)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
