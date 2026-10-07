"""
bot.py —— Telegram 介面：收訊息、白名單、按鈕、把工作交給工具層／大腦

用口語說就好，例如：
  現在有哪些 CLI？
  競賽那個在幹嘛？
  關注競賽那個，結束跟我說
  跟競賽那個說：改用 v2 資料集
  幫我貼上接下來這段訊息   （下一則訊息會原封不動貼進去）
  開一個新的 CLI 在 ~/projects/thesis，幫我整理 related work
  競賽那個如果在問要不要允許，就幫我按允許
  > 繼續                    （以 > 開頭：原文直接送進「目前對象」，不經過大腦）

斜線指令（備用，不經過大腦）：
  /list                        列出所有 CLI
  /use 名稱                    設定目前對象
  /look [名稱] [行數]           看畫面原文
  /send 名稱 訊息               送一段訊息
  /paste [名稱]                 下一則訊息原封不動貼進去
  /watch 名稱　/unwatch 名稱    關注／取消關注
  /key 名稱 按鍵                送按鍵（1 2 3 y n enter esc up down tab ctrl-c），會先問你
  /new 名稱 資料夾 [第一個任務]  開新的 CLI
  /kill 名稱                    關閉 CLI，會先問你
  /cancel                      取消「等待貼上」
"""

from __future__ import annotations

import asyncio
import collections
import functools
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import Conflict, NetworkError, TelegramError, TimedOut
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from . import safety, tmux_ops
from .brain import Brain, BrainError
from .config import Config
from .monitor import Monitor
from .state import State
from .tools import READ_ONLY_TOOLS, Choice, Pending, ToolBox, ToolResult

log = logging.getLogger(__name__)

MAX_TG = 3900               # Telegram 單則上限 4096 字，留一點餘裕
CHOICE_TIMEOUT = 120        # 「選哪一個」按鈕的有效時間
RAW_BUTTON_TIMEOUT = 86400  # 「📄 原文」按鈕保留一天
MAX_BRAIN_ROUNDS = 3        # 大腦最多來回幾輪（看畫面 → 再決定）
HISTORY_TURNS = 8           # 給大腦參考的最近對話則數
ALERT_INTERVAL = 300        # 同一類錯誤幾秒內只通知一次，避免洗版
STALE_MESSAGE = 300         # 超過幾秒的舊訊息不執行（bot 停機期間累積的訊息，重開後不該突然照做）

HELP = (__doc__ or "").split("用口語說就好", 1)[1].strip()
HELP = "用口語說就好" + HELP

COMMANDS = [
    ("list", "列出所有 CLI"), ("use", "設定目前對象"), ("look", "看畫面原文"), ("send", "送一段訊息"),
    ("paste", "下一則訊息原封不動貼進去"), ("watch", "關注"), ("unwatch", "取消關注"),
    ("key", "送按鍵（會先問你）"), ("new", "開新的 CLI"), ("kill", "關閉 CLI（會先問你）"),
    ("cancel", "取消等待貼上"), ("help", "說明"),
]

ButtonAction = Callable[[], Awaitable[Any]]


@dataclass
class ButtonSet:
    """一則訊息上的一組按鈕。"""

    chat_id: int
    text: str
    options: list[tuple[str, ButtonAction | None]]   # (按鈕文字, 按下後執行的動作；None＝取消)
    single_use: bool = True                          # 按過一次就失效（確認類按鈕）
    message_id: int | None = None
    group: str | None = None                         # 同一群組只保留最新的一組（例如同一個 CLI 的權限確認）


def split_message(text: str, limit: int = MAX_TG) -> list[str]:
    """把長文字切成 Telegram 能傳的段落，盡量在換行處切。"""
    text = text or "（空白）"
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


def authorized_only(func: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """
    裝飾器：只有白名單內的使用者才能執行。
    其他人只會收到「未授權」和他自己的 user id，不洩漏任何系統資訊（PROMPT.md 第 6 節第 1 點）。
    """
    @functools.wraps(func)
    async def wrapper(self: "ZueBot", update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in self.cfg.allowed_user_ids:
            log.info("拒絕未授權使用者 %s", user.id if user else "未知")
            if update.callback_query:
                await update.callback_query.answer("未授權")
            elif update.effective_message:
                await update.effective_message.reply_text(f"未授權。你的 user id 是 {user.id if user else '未知'}")
            return
        msg = update.effective_message if update.callback_query is None else None
        sent_at = getattr(msg, "date", None)
        if sent_at is not None and time.time() - sent_at.timestamp() > STALE_MESSAGE:
            minutes = int((time.time() - sent_at.timestamp()) // 60)
            await msg.reply_text(f"⏸ 這則訊息是 {minutes} 分鐘前（bot 沒在運作時）傳的，為了安全沒有執行。需要的話請再傳一次。")
            return
        try:
            await func(self, update, ctx)
        except tmux_ops.TmuxError as e:
            # 斜線指令遇到 tmux 錯誤（例如 CLI 正在等確認、找不到 session）：直接回一句人話
            if update.effective_chat:
                await self.notify(update.effective_chat.id, f"❌ {e}")
    return wrapper


class ZueBot:
    """把設定、狀態、工具層、大腦、監控與 Telegram 指令包在一起的主類別。"""

    def __init__(self, cfg: Config, state: State) -> None:
        """建立各個元件。Application 在 build_application() 才建立。"""
        self.cfg = cfg
        self.state = state
        self.app: Application | None = None
        self.toolbox = ToolBox(cfg, state, self)
        self.brain = Brain(cfg)
        self.monitor = Monitor(cfg, state, self.toolbox, self.brain, self, on_error=self.alert)
        self._last_alert: dict[str, float] = {}
        self.buttons: dict[str, ButtonSet] = {}
        self.locks: dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self.history: dict[int, collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=HISTORY_TURNS))
        self._monitor_task: asyncio.Task | None = None

    # ───────────── 傳訊息（所有往 Telegram 的訊息都經過這裡，統一遮蔽機密）─────────────
    async def notify(self, chat_id: int, text: str) -> None:
        """傳訊息給你，太長自動切段，送出前遮蔽機密。"""
        for chunk in split_message(safety.mask_secrets(text)):
            await self.app.bot.send_message(chat_id=chat_id, text=chunk)

    async def show_buttons(self, chat_id: int, text: str, options: list[tuple[str, ButtonAction | None]],
                           timeout: float, per_row: int = 1, single_use: bool = True,
                           expire_note: str = "⌛ 已逾時，自動取消", group: str | None = None) -> None:
        """
        傳一則附按鈕的訊息。timeout 秒後按鈕自動失效，並在訊息後面註明。
        callback_data 只放短 id（Telegram 限制 64 bytes），實際動作存在記憶體裡。
        group：同一群組的舊按鈕會立刻失效（例如同一個 CLI 又跳出新的權限確認，舊按鈕就不能再按）。
        """
        if group:
            for old_id, old in list(self.buttons.items()):
                if old.group == group:
                    self.buttons.pop(old_id, None)
                    await self._mark_expired(old, "⌛ 已經有新的確認畫面，這組按鈕已失效")
        bid = uuid.uuid4().hex[:12]
        chunks = split_message(safety.mask_secrets(text))
        for chunk in chunks[:-1]:
            await self.app.bot.send_message(chat_id=chat_id, text=chunk)
        rows, row = [], []
        for i, (label, _) in enumerate(options):
            row.append(InlineKeyboardButton(label[:60], callback_data=f"b:{bid}:{i}"))
            if len(row) >= per_row:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        msg = await self.app.bot.send_message(chat_id=chat_id, text=chunks[-1], reply_markup=InlineKeyboardMarkup(rows))
        self.buttons[bid] = ButtonSet(chat_id, chunks[-1], options, single_use, msg.message_id, group)
        self.toolbox.spawn(self._expire_buttons(bid, timeout, expire_note))

    async def _expire_buttons(self, bid: str, timeout: float, note: str) -> None:
        """逾時後讓按鈕失效：移除按鈕並在訊息後面加上說明。"""
        await asyncio.sleep(timeout)
        bs = self.buttons.pop(bid, None)
        if bs:
            await self._mark_expired(bs, note)

    async def _mark_expired(self, bs: ButtonSet, note: str) -> None:
        """把按鈕從訊息上拿掉，並在訊息後面加上說明（失敗就算了，不影響其他事）。"""
        if not bs.message_id:
            return
        try:
            await self.app.bot.edit_message_text(chat_id=bs.chat_id, message_id=bs.message_id,
                                                 text=f"{bs.text}\n\n{note}" if note else bs.text)
        except TelegramError:
            pass

    async def confirm(self, chat_id: int, pending: Pending) -> None:
        """顯示「✅ 同意／❌ 取消」確認按鈕（PROMPT.md 第 6 節第 2 點）。"""
        seconds = int(pending.timeout)
        await self.show_buttons(
            chat_id, f"⚠️ 需要你確認\n\n{pending.title}\n\n（{seconds} 秒內沒按會自動取消）",
            [("✅ 同意", pending.run), ("❌ 取消", None)], pending.timeout, per_row=2)

    async def ask_choice(self, chat_id: int, choice: Choice) -> None:
        """名稱有歧義時，列出候選按鈕讓你選。"""
        options: list[tuple[str, ButtonAction | None]] = [
            (opt, functools.partial(choice.run, opt)) for opt in choice.options]
        options.append(("❌ 都不是", None))
        await self.show_buttons(chat_id, f"🤔 {choice.question}", options, CHOICE_TIMEOUT)

    async def notify_with_raw(self, chat_id: int, text: str, raw: str) -> None:
        """傳訊息並附「📄 原文」按鈕（可以重複按），按了會傳完整原文。"""
        async def show() -> str:
            return f"📄 完整回覆：\n\n{raw}"
        await self.show_buttons(chat_id, text, [("📄 原文", show)], RAW_BUTTON_TIMEOUT,
                                single_use=False, expire_note="")

    async def present(self, chat_id: int, result: Any) -> None:
        """把工具結果呈現給你：文字、確認按鈕或選擇按鈕。"""
        if result is None:
            return
        if isinstance(result, str):
            await self.notify(chat_id, result)
            return
        if isinstance(result, ToolResult):
            if result.message:
                await self.notify(chat_id, result.message)
            if result.pending:
                await self.confirm(chat_id, result.pending)
            if result.choice:
                await self.ask_choice(chat_id, result.choice)

    # ───────────── 按鈕被按下 ─────────────
    @authorized_only
    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """處理按鈕：找出對應的動作並執行，確認類按鈕按過就失效。"""
        query = update.callback_query
        try:
            _, bid, idx = (query.data or "").split(":")
            bs = self.buttons.get(bid)
            label, action = bs.options[int(idx)] if bs else ("", None)
        except (ValueError, IndexError):
            bs = None
        # 重要：在任何 await 之前就把單次按鈕取走。bot 會同時處理多個更新，
        # 如果先 await 再取走，連點兩下（或先按同意再按取消）會讓動作執行兩次。
        if bs is not None and bs.single_use and self.buttons.pop(bid, None) is None:
            bs = None
        if bs is None:
            await query.answer("這個按鈕已經失效了")
            return
        await query.answer()
        if bs.single_use:
            note = "❌ 已取消" if action is None else f"👉 你選了：{label}"
            try:
                await query.edit_message_text(f"{bs.text}\n\n{note}")
            except TelegramError:
                pass
        if action is None:
            return
        try:
            result = await action()
        except tmux_ops.TmuxError as e:
            result = f"❌ {e}"
        except Exception as e:
            log.exception("按鈕動作失敗")
            result = f"❌ 執行時發生錯誤：{e}"
        await self.present(bs.chat_id, result)

    # ───────────── 斜線指令（備用，不經過大腦）─────────────
    def _arg_text(self, update: Update, parts: int) -> list[str]:
        """把指令訊息切成最多 parts 段（最後一段保留原本的空白和換行）。"""
        return (update.effective_message.text or "").split(maxsplit=parts - 1)

    @authorized_only
    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/start、/help：顯示說明。"""
        await self.notify(update.effective_chat.id, HELP)

    @authorized_only
    async def cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/list：列出所有 CLI。"""
        await self.present(update.effective_chat.id, await self.toolbox.list_clis(update.effective_chat.id))

    @authorized_only
    async def cmd_use(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/use 名稱：設定目前對象。"""
        chat_id = update.effective_chat.id
        await self.present(chat_id, await self.toolbox.set_current(ctx.args[0] if ctx.args else "", chat_id))

    @authorized_only
    async def cmd_look(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/look [名稱] [行數]：看畫面原文。只給數字時當作行數，對象用目前對象。"""
        chat_id = update.effective_chat.id
        args = list(ctx.args)
        lines = int(args.pop()) if args and args[-1].isdigit() else 40
        name = args[0] if args else (self.state.get_current(chat_id) or "")
        await self.present(chat_id, await self.toolbox.show_raw(name, lines))

    @authorized_only
    async def cmd_send(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/send 名稱 訊息：送一段訊息（危險字眼仍會先問你）。"""
        parts = self._arg_text(update, 3)
        if len(parts) < 3:
            await self.notify(update.effective_chat.id, "用法：/send <名稱> <訊息>")
            return
        chat_id = update.effective_chat.id
        await self.present(chat_id, await self.toolbox.send_text(parts[1], parts[2], chat_id))

    @authorized_only
    async def cmd_paste(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/paste [名稱]：下一則訊息原封不動貼進去。"""
        chat_id = update.effective_chat.id
        name = ctx.args[0] if ctx.args else (self.state.get_current(chat_id) or "")
        await self.present(chat_id, await self.toolbox.arm_paste(name, chat_id))

    @authorized_only
    async def cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/cancel：取消等待貼上。"""
        name, _ = self.toolbox.take_paste(update.effective_chat.id)
        await self.notify(update.effective_chat.id, f"已取消貼進 [{name}]" if name else "目前沒有在等待貼上")

    @authorized_only
    async def cmd_watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/watch 名稱：開始關注。"""
        chat_id = update.effective_chat.id
        await self.present(chat_id, await self.toolbox.watch(ctx.args[0] if ctx.args else "", chat_id))

    @authorized_only
    async def cmd_unwatch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/unwatch 名稱：取消關注。"""
        await self.present(update.effective_chat.id, await self.toolbox.unwatch(ctx.args[0] if ctx.args else ""))

    @authorized_only
    async def cmd_key(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/key 名稱 按鍵：送按鍵，會先用按鈕問你。"""
        chat_id = update.effective_chat.id
        if len(ctx.args) != 2:
            await self.notify(chat_id, f"用法：/key <名稱> <{' / '.join(tmux_ops.KEYS)}>")
            return
        await self.present(chat_id, await self.toolbox.send_key(ctx.args[0], ctx.args[1], chat_id))

    @authorized_only
    async def cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/new 名稱 資料夾 [第一個任務]：開新的 CLI。"""
        parts = self._arg_text(update, 4)
        chat_id = update.effective_chat.id
        if len(parts) < 3:
            await self.notify(chat_id, "用法：/new <名稱> <資料夾> [第一個任務]")
            return
        first = parts[3] if len(parts) > 3 else ""
        await self.present(chat_id, await self.toolbox.new_cli(parts[1], parts[2], first, chat_id))

    @authorized_only
    async def cmd_kill(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/kill 名稱：關閉 CLI，會先用按鈕問你。"""
        chat_id = update.effective_chat.id
        if not ctx.args:
            await self.notify(chat_id, "用法：/kill <名稱>")
            return
        await self.present(chat_id, await self.toolbox.kill_cli(ctx.args[0], chat_id))

    # ───────────── 一般文字 ─────────────
    @authorized_only
    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """
        一般文字訊息。同一個聊天室的訊息依序處理（用鎖），
        這樣「幫我貼上接下來這段訊息」之後的下一則，一定會等前一則處理完才判斷要不要貼上。
        """
        chat_id = update.effective_chat.id
        text = update.effective_message.text or ""
        async with self.locks[chat_id]:
            try:
                await self._handle_text(chat_id, text)
            except Exception as e:
                log.exception("處理訊息失敗")
                await self.notify(chat_id, f"😵 處理這則訊息時出錯了：{e}\n可以改用斜線指令，例如 /list、/look 名稱。")

    async def _handle_text(self, chat_id: int, text: str) -> None:
        """依序判斷：等待貼上 → 以 > 開頭直接送 → 交給大腦（或關掉大腦時送進目前對象）。"""
        name, expired = self.toolbox.take_paste(chat_id)
        if name:
            await self.present(chat_id, await self.toolbox.send_text(name, text, chat_id))
            return
        if expired:
            await self.notify(chat_id, "（貼上已逾時，這則訊息改照一般方式處理）")
        if text.startswith(">"):
            body = text[1:].lstrip(" ")
            current = self.state.get_current(chat_id)
            if not current:
                await self.notify(chat_id, "還沒設定目前對象。先說「把競賽設為目前對象」或用 /use 名稱。")
                return
            await self.present(chat_id, await self.toolbox.send_text(current, body, chat_id))
            return
        if not self.cfg.brain_enabled:
            current = self.state.get_current(chat_id)
            if not current:
                await self.notify(chat_id, "大腦已關閉（BRAIN_ENABLED=0）。先 /use 名稱 設定目前對象，或用斜線指令。")
                return
            await self.present(chat_id, await self.toolbox.send_text(current, text, chat_id))
            return
        await self.think(chat_id, text)

    async def _typing(self, chat_id: int, stop: asyncio.Event) -> None:
        """大腦思考時，持續顯示「輸入中…」（Telegram 的狀態只維持 5 秒，所以每 4 秒送一次）。"""
        while not stop.is_set():
            try:
                await self.app.bot.send_chat_action(chat_id, ChatAction.TYPING)
            except TelegramError:
                pass
            try:
                await asyncio.wait_for(stop.wait(), 4)
            except asyncio.TimeoutError:
                pass

    async def build_context(self, chat_id: int) -> dict[str, Any]:
        """給大腦的背景資訊：所有 CLI 與狀態、目前對象、家目錄、允許的資料夾。"""
        listing = await self.toolbox.list_clis(chat_id)
        return {"clis": listing.data or [], "current": self.state.get_current(chat_id),
                "home": str(Path.home()), "allowed_root": str(self.cfg.allowed_root)}

    async def think(self, chat_id: int, text: str) -> None:
        """
        交給大腦處理一則口語訊息：
          1. 大腦可能先要求看畫面（read_screen，done=false）→ bot 讀完把結果給大腦，再問一次（最多 3 輪）
          2. 最後的計畫裡的動作，逐一交給工具層驗證並執行；需要確認的會跳按鈕
        """
        stop = asyncio.Event()
        typing = asyncio.create_task(self._typing(chat_id, stop))
        try:
            context = await self.build_context(chat_id)
            history = list(self.history[chat_id])
            observations: list[dict[str, Any]] = []
            plan = None
            for round_no in range(MAX_BRAIN_ROUNDS):
                plan = await self.brain.plan(text, context, history, observations)
                reads = [a for a in plan.actions if a.get("tool") in READ_ONLY_TOOLS]
                if plan.done or not reads or round_no == MAX_BRAIN_ROUNDS - 1:
                    break
                for action in reads[:4]:
                    res = await self.toolbox.call(action, chat_id, text)
                    if res.choice:
                        observed: Any = f"名稱有歧義，候選：{'、'.join(res.choice.options)}"
                    else:
                        observed = res.data if res.data is not None else res.message
                    observations.append({"tool": action.get("tool"), "name": action.get("name", ""), "result": observed})
        except BrainError as e:
            await self.notify(chat_id, f"🤖 大腦暫時沒辦法處理（{e}）。\n可以改用斜線指令：/list 看 CLI、/look 名稱 看畫面、/send 名稱 訊息 傳話。")
            return
        finally:
            stop.set()
            await typing

        if plan.reply:
            await self.notify(chat_id, plan.reply)
        self.history[chat_id].append(("使用者", text))
        self.history[chat_id].append(("zuebot", plan.reply or "（執行動作）"))
        executed = 0
        for action in plan.actions:
            tool = action.get("tool")
            if tool == "read_screen" or (tool == "list_clis" and plan.reply):
                continue        # 畫面是給大腦看的；清單已經寫在回覆裡
            executed += 1
            await self.present(chat_id, await self.toolbox.call(action, chat_id, text))
        if not plan.reply and not executed:
            await self.notify(chat_id, "🤔 我不太確定你要我做什麼，可以再說清楚一點嗎？")

    # ───────────── 錯誤通知（PROMPT.md Phase 4：bot 不能默默掛掉）─────────────
    async def alert(self, text: str, key: str | None = None) -> None:
        """
        把系統層級的問題用一句人話通知所有白名單使用者。
        同一類（key）的錯誤 ALERT_INTERVAL 秒內只通知一次；通知本身失敗也不會再丟錯。
        """
        key = key or text[:40]
        now = time.time()
        if now - self._last_alert.get(key, 0) < ALERT_INTERVAL:
            return
        self._last_alert[key] = now
        for uid in self.cfg.allowed_user_ids:
            try:
                await self.notify(uid, f"⚠️ {text}")
            except Exception:
                log.warning("錯誤通知送不出去", exc_info=True)

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """全域錯誤處理：記到 log，並視情況通知你。網路暫時斷線只記 log（python-telegram-bot 會自己重試）。"""
        err = ctx.error
        if isinstance(err, Conflict):
            log.error("409 Conflict：同一個 token 有另一個程式在收訊息")
            await self.alert("同一個 bot token 有另一個程式也在收訊息（409 Conflict）。"
                             "可能是手動開了一個、launchd 又開了一個，請關掉其中一個。", key="conflict")
            return
        if isinstance(err, (NetworkError, TimedOut)):
            log.warning("網路暫時有問題：%s", err)
            return
        log.error("處理更新時發生錯誤", exc_info=err)
        await self.alert(f"zuebot 遇到未預期的錯誤：{type(err).__name__}: {err}。bot 還在運作；"
                         f"如果一直出現，請看 ~/.zuebot/logs/bot.log", key=type(err).__name__)

    # ───────────── 生命週期 ─────────────
    async def post_init(self, app: Application) -> None:
        """bot 連上 Telegram 後執行：設定指令選單、啟動背景監控、通知你 bot 已啟動。"""
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except TelegramError:
            log.warning("設定指令選單失敗（不影響使用）", exc_info=True)
        self._monitor_task = asyncio.create_task(self.monitor.run())
        if self.cfg.startup_notify:
            watching = "、".join(self.state.watches) or "（沒有）"
            for uid in self.cfg.allowed_user_ids:
                try:
                    await self.notify(uid, f"🤖 zuebot 已啟動。關注中：{watching}\n傳「現在有哪些 CLI？」或 /help 開始。")
                except TelegramError:
                    log.warning("啟動通知送不出去（可能你還沒對 bot 按過開始）", exc_info=True)

    async def post_shutdown(self, app: Application) -> None:
        """bot 關閉時執行：停止背景監控、存檔。"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
        self.state.save()

    def build_application(self) -> Application:
        """建立 Telegram Application 並註冊所有指令與按鈕處理。"""
        # concurrent_updates：大腦思考時（可能十幾秒）按鈕和其他訊息仍然可以處理
        app = (Application.builder().token(self.cfg.bot_token).concurrent_updates(True)
               .post_init(self.post_init).post_shutdown(self.post_shutdown).build())
        self.app = app
        handlers = [("start", self.cmd_help), ("help", self.cmd_help), ("list", self.cmd_list),
                    ("use", self.cmd_use), ("look", self.cmd_look), ("send", self.cmd_send),
                    ("paste", self.cmd_paste), ("cancel", self.cmd_cancel), ("watch", self.cmd_watch),
                    ("unwatch", self.cmd_unwatch), ("key", self.cmd_key), ("new", self.cmd_new),
                    ("kill", self.cmd_kill)]
        # 只處理「新訊息」：你在 Telegram 編輯舊訊息時（例如修錯字），不會再執行一次
        new_only = filters.UpdateType.MESSAGE
        for cmd, fn in handlers:
            app.add_handler(CommandHandler(cmd, fn, filters=new_only))
        app.add_handler(CallbackQueryHandler(self.on_button, pattern=r"^b:"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & new_only, self.on_text))
        app.add_error_handler(self.on_error)
        return app


def run(cfg: Config) -> None:
    """讀取狀態、建立 bot 並開始 polling（會一直執行到按 Ctrl-C）。"""
    if not cfg.allowed_user_ids:
        log.warning("⚠️ 還沒設定 ALLOWED_USER_IDS：先傳任何訊息給 bot 取得你的 user id，填進 .env 後重開。")
    state = State.load(cfg.state_file)
    bot = ZueBot(cfg, state)
    app = bot.build_application()
    log.info("Bot 啟動。允許的資料夾根目錄：%s；事件檔：%s；大腦：%s",
             cfg.allowed_root, cfg.events_file, f"claude -p（{cfg.brain_model}）" if cfg.brain_enabled else "關閉")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
