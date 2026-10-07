"""
monitor.py —— 背景監控：讀 hook 事件、完成回報、權限通知

  - 每 2 秒讀一次事件檔（hook.py 寫的）
  - 關注中的 CLI 完成一輪（Stop）→ 交給大腦整理成 3～5 句的摘要通知你，附「📄 原文」按鈕
  - 需要你注意（Notification）→ 通知你並附上畫面最後幾行

為了在 10 秒內收到完成回報：摘要用比較快的模型（SUMMARY_MODEL，預設 haiku），
回覆本來就很短時直接轉給你；摘要超過 QUICK_NOTICE_AFTER 秒還沒好，先送一則「完成了，摘要整理中」。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Protocol

from . import cli, safety, screen, tmux_ops
from .brain import Brain, BrainError
from .config import Config
from .events import EventReader
from .state import State
from .tmux_ops import TmuxError
from .tools import ToolBox

log = logging.getLogger(__name__)

EVENT_POLL_SECONDS = 2       # 每幾秒讀一次事件檔
SHORT_REPLY = 300            # 回覆比這個短就不用摘要，直接轉給你
QUICK_NOTICE_AFTER = 6       # 摘要超過幾秒還沒好，先送一則簡短通知
PERMISSION_DEDUP = 90        # 同一個權限畫面在幾秒內只通知一次（PermissionRequest 和 Notification 都會來）


class MonitorUI(Protocol):
    """監控需要的對外溝通能力，由 bot 實作。"""

    async def notify(self, chat_id: int, text: str) -> None:
        """傳一則訊息。"""

    async def notify_with_raw(self, chat_id: int, text: str, raw: str) -> None:
        """傳一則訊息，附上「📄 原文」按鈕，按了會傳 raw。"""

    async def show_buttons(self, chat_id: int, text: str,
                           options: list[tuple[str, Callable[[], Awaitable[Any]] | None]],
                           timeout: float, per_row: int = 1, single_use: bool = True,
                           expire_note: str = "") -> None:
        """傳一則附按鈕的訊息。"""


class Monitor:
    """背景監控的主體，由 bot 啟動後一直執行。"""

    def __init__(self, cfg: Config, state: State, toolbox: ToolBox, brain: Brain, ui: MonitorUI) -> None:
        """準備事件讀取器與同時摘要數量的限制（避免一次開太多 claude -p）。"""
        self.cfg = cfg
        self.state = state
        self.toolbox = toolbox
        self.brain = brain
        self.ui = ui
        self.reader = EventReader(cfg.events_file, state.events_offset)
        self.summary_slots = asyncio.Semaphore(2)
        self.last_permission: dict[str, tuple[int, float]] = {}   # session → (畫面雜湊, 通知時間)

    async def run(self) -> None:
        """主迴圈：一直讀事件並處理。任何錯誤只記 log，迴圈不會停。"""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("監控迴圈發生錯誤")
            await asyncio.sleep(EVENT_POLL_SECONDS)

    async def tick(self) -> None:
        """執行一輪：讀新事件、存下讀取進度、逐一處理。"""
        events = self.reader.read_new()
        if self.reader.offset != self.state.events_offset:
            self.state.events_offset = self.reader.offset
            self.state.save()
        for ev in events:
            log.debug("hook 事件：%s", ev)
            try:
                await self.handle_event(ev)
            except Exception:
                log.exception("處理事件失敗：%s", ev)

    async def handle_event(self, ev: dict[str, Any]) -> None:
        """處理一個 hook 事件：找出是哪個 CLI、有沒有人關注，再決定怎麼通知。"""
        name = await tmux_ops.session_of_pane(str(ev.get("pane", "")))
        if not name:
            return                                  # 那個 pane 已經不在了
        chat_id = self.state.watcher(name)
        if not chat_id:
            return                                  # 沒在關注的 CLI 不打擾你
        kind = ev.get("event")
        if kind == "Stop":
            self.toolbox.awaiting.pop(name, None)
            self.toolbox.spawn(self.report_completion(name, chat_id, str(ev.get("reply") or ""), str(ev.get("cwd") or "")))
        elif kind == "PermissionRequest":
            self.toolbox.spawn(self.notify_permission(name, chat_id, ev))
        elif kind == "Notification":
            ntype = ev.get("notification_type")
            if ntype == "idle_prompt":
                return   # 閒置約 60 秒的提醒：Stop 時已經通知過完成了，不重複打擾
            if ntype == "permission_prompt":
                self.toolbox.spawn(self.notify_permission(name, chat_id, ev))
                return
            await self.notify_attention(name, chat_id, ev)

    async def notify_permission(self, name: str, chat_id: int, ev: dict[str, Any]) -> None:
        """
        權限確認通知（PROMPT.md Phase 3）：轉述它想做什麼、附畫面最後幾行，
        再依畫面上的選項產生按鈕。你按了按鈕才會送出按鍵（按鈕本身就是確認）。
        """
        state, text = screen.UNKNOWN, ""
        for _ in range(4):                       # 畫面可能還沒畫完，最多等 4 秒
            await asyncio.sleep(1)
            try:
                state, text = await cli.get_state(name, 40)
            except TmuxError:
                return
            if state == screen.PERMISSION:
                break
        if state != screen.PERMISSION and ev.get("event") == "Notification":
            return                               # 已經不是確認畫面（你可能已經在電腦上處理了）
        tail = screen.recent(text, 15)
        digest = hash(tail)
        last = self.last_permission.get(name)
        if last and last[0] == digest and time.time() - last[1] < PERMISSION_DEDUP:
            return                               # 同一個畫面已經通知過
        self.last_permission[name] = (digest, time.time())

        if ev.get("event") == "PermissionRequest":
            tool_input = str(ev.get("tool_input") or "")
            detail = f"它想使用 {ev.get('tool_name') or '某個工具'}：{tool_input[:400]}"
        else:
            detail = str(ev.get("message") or "它在等你確認")
        options = screen.menu_options(text) or [("1", "Yes（允許）"), ("2", "Yes, and don't ask again（允許且不再詢問）")]
        buttons: list[tuple[str, Callable[[], Awaitable[Any]] | None]] = []
        for key, label in options:
            if label.lower().startswith("no"):
                continue                         # 「No」用下面的 Esc 按鈕
            buttons.append((f"{'✅' if key == '1' else '☑️'} {key}. {label[:40]}",
                            lambda key=key, label=label: self.answer_permission(name, chat_id, key, label)))
        buttons.append(("❌ 拒絕（Esc）", lambda: self.answer_permission(name, chat_id, "esc", "拒絕")))
        minutes = max(1, int(self.cfg.permission_button_timeout // 60))
        await self.ui.show_buttons(
            chat_id,
            f"🔔 [{name}] 在等你確認\n{detail}\n\n{tail}\n\n按下面的按鈕回應（{minutes} 分鐘內有效，按了才會送出按鍵）",
            buttons, self.cfg.permission_button_timeout,
            expire_note="⌛ 按鈕已失效。若它還在等，可以說「幫我看一下它在問什麼」。")

    async def answer_permission(self, name: str, chat_id: int, key: str, label: str) -> str:
        """按下權限按鈕後執行：再確認一次畫面還是權限確認，才送出按鍵。"""
        state, _ = await cli.get_state(name)
        if state != screen.PERMISSION:
            return f"[{name}] 已經不是權限確認畫面了（可能已經在電腦上處理過），沒有送出任何按鍵。"
        await tmux_ops.send_key(name, key)
        self.toolbox.awaiting[name] = time.time()
        await asyncio.sleep(1.5)
        after = safety.mask_secrets(screen.recent(await tmux_ops.capture(name, 30), 8))
        return f"⌨️ 已對 [{name}] 選擇「{label}」，它會繼續執行，完成時會通知你。\n\n{after}"

    async def notify_attention(self, name: str, chat_id: int, ev: dict[str, Any]) -> None:
        """CLI 需要你注意（例如在等你確認）：通知你並附上畫面最後幾行。"""
        try:
            tail = safety.mask_secrets(screen.recent(await tmux_ops.capture(name, 30), 15))
        except TmuxError as e:
            tail = f"（{e}）"
        message = safety.mask_secrets(str(ev.get("message") or "需要你處理"))
        await self.ui.notify(chat_id, f"🔔 [{name}] 在等你：{message}\n\n{tail}\n\n"
                                      f"可以說「{name} 那個幫我按允許」，或用 /key {name} 1")

    async def report_completion(self, name: str, chat_id: int, reply: str, cwd: str) -> None:
        """完成回報：短回覆直接轉；長回覆交給大腦摘要，並附「📄 原文」按鈕。"""
        raw = safety.mask_secrets(reply.strip())
        try:
            tail = safety.mask_secrets(screen.recent(await tmux_ops.capture(name, 40), 25))
        except TmuxError:
            tail = ""
        if not raw:
            raw = f"（抓不到回覆文字，以下是畫面）\n\n{tail}"
        if not self.cfg.brain_enabled or len(raw) <= SHORT_REPLY:
            await self.ui.notify_with_raw(chat_id, f"✅ [{name}] 這輪完成：\n\n{raw}", raw)
            return
        summary_task = asyncio.create_task(self._summarize(name, cwd, raw, tail))
        done, _ = await asyncio.wait({summary_task}, timeout=QUICK_NOTICE_AFTER)
        if not done:
            await self.ui.notify(chat_id, f"✅ [{name}] 這輪完成了，摘要整理中…")
        summary = await summary_task
        if summary is None:
            short = raw if len(raw) <= 1500 else raw[:1500] + "…（按「📄 原文」看完整內容）"
            await self.ui.notify_with_raw(chat_id, f"✅ [{name}] 這輪完成（摘要失敗，附上回覆）：\n\n{short}", raw)
        else:
            await self.ui.notify_with_raw(chat_id, f"✅ [{name}] 這輪完成\n\n{summary}", raw)

    async def _summarize(self, name: str, cwd: str, raw: str, tail: str) -> str | None:
        """呼叫大腦摘要；失敗回傳 None（由呼叫端改送原文）。"""
        async with self.summary_slots:
            try:
                return safety.mask_secrets(await self.brain.summarize(name, cwd, raw, tail, "這一輪工作剛完成"))
            except BrainError as e:
                log.warning("摘要失敗：%s", e)
                return None
