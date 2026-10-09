"""
tools.py —— 工具層：bot 和大腦「唯一」能用來操作 CLI 的方式

PROMPT.md 第 5 節的工具都在這裡：
  list_clis  read_screen  show_raw  send_text  send_key  watch  unwatch
  set_current  new_cli  arm_paste  kill_cli

設計原則：
  1. 每個工具自己驗證參數（名稱存不存在、路徑在不在允許範圍、按鍵在不在白名單）。
     大腦給的參數一律當作「不可信任」，就算大腦出錯也不會造成危險。
  2. 需要你確認的動作不會直接執行，而是回傳 Pending，由 bot 顯示「✅ 同意／❌ 取消」按鈕。
  3. 名稱有歧義（例如兩個都像「demo」）時回傳 Choice，由 bot 列出候選讓你選，絕不自己猜。
  4. 大腦說要轉貼的文字，必須是你原訊息裡一字不差的片段，否則先問你（「原話轉貼」規則）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from . import cli, safety, screen, tmux_ops
from .config import Config
from .state import State
from .tmux_ops import TmuxError

log = logging.getLogger(__name__)

PASTE_TIMEOUT = 300        # 「貼上下一則訊息」的有效時間（秒）
KEY_LABELS = {"1": "1（通常是「是／允許」）", "2": "2（通常是「是，而且不再詢問」）", "3": "3（通常是「否」）",
              "enter": "Enter", "esc": "Esc（通常是「拒絕／取消」）", "y": "y", "n": "n",
              "up": "↑", "down": "↓", "tab": "Tab", "ctrl-c": "Ctrl-C（中斷）"}
READ_ONLY_TOOLS = {"list_clis", "read_screen"}


@dataclass
class Pending:
    """需要你按「✅ 同意」才會執行的動作。"""

    title: str                                  # 確認訊息：說明要做什麼、為什麼要確認
    run: Callable[[], Awaitable[str]]           # 同意後執行，回傳結果訊息
    timeout: float = 60                         # 幾秒內沒回應就自動取消


@dataclass
class Choice:
    """名稱有歧義時，請你從候選中選一個。選好後用選中的名稱執行 run。"""

    question: str
    options: list[str]
    run: Callable[[str], Awaitable["ToolResult"]]


@dataclass
class ToolResult:
    """工具執行的結果。"""

    message: str = ""                           # 給你看的訊息
    data: Any = None                            # 給大腦看的資料（只有查詢類工具會有）
    pending: Pending | None = None              # 需要確認的動作
    choice: Choice | None = None                # 需要你選擇
    ok: bool = True


class UI(Protocol):
    """工具層需要的「對外溝通」能力，由 bot 實作。背景工作（例如等新 CLI 啟動）會用到。"""

    async def notify(self, chat_id: int, text: str) -> None:
        """傳一則訊息給你。"""

    async def confirm(self, chat_id: int, pending: Pending) -> None:
        """顯示確認按鈕。"""


def is_verbatim(text: str, source: str) -> bool:
    """
    「原話轉貼」檢查：text 是否為 source（你的原訊息）裡一字不差的連續片段。
    只容許頭尾的空白不同；中間任何一個字不同都算不是原話。
    """
    text = text.strip()
    return bool(text) and text in source


def safe_session_name(raw: str) -> str:
    """把任意字串整理成合法的 session 名稱（去掉空白、冒號、句點等），最多 30 字。"""
    cleaned = re.sub(r"[^\w\-]", "-", raw.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[:30]


class ToolBox:
    """所有工具的實作。bot 的斜線指令和大腦都透過這裡操作 CLI。"""

    def __init__(self, cfg: Config, state: State, ui: UI) -> None:
        """準備工具層需要的設定、狀態與對外溝通介面。"""
        self.cfg = cfg
        self.state = state
        self.ui = ui
        self.paste_armed: dict[int, tuple[str, float]] = {}     # chat_id → (session, 到期時間)
        self.awaiting: dict[str, float] = {}                     # session → 送出訊息的時間（等它完成）
        self._background: set[asyncio.Task] = set()              # 背景工作，保留參照避免被回收

    # ───────────── 共用 ─────────────
    def spawn(self, coro: Awaitable[None]) -> None:
        """開一個背景工作，錯誤只記 log，不影響 bot。"""
        async def guarded() -> None:
            try:
                await coro
            except Exception:
                log.exception("背景工作失敗")
        task = asyncio.create_task(guarded())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def resolve(self, query: str) -> tuple[str | None, list[str], str]:
        """
        把「你說的名稱」對應到實際的 session，回傳 (名稱, 候選清單, 錯誤訊息)。
          1. 完全相同 → 直接用
          2. 名稱包含關鍵字（不分大小寫）或工作目錄的資料夾名稱相同 → 只有一個就用它，多個就列出候選
          3. 都沒有 → 回傳錯誤訊息並列出現有的 CLI
        """
        query = (query or "").strip()
        sessions = await tmux_ops.list_sessions()
        names = [s.name for s in sessions]
        if not query:
            return None, [], f"沒有指定是哪個 CLI。現有：{'、'.join(names) or '（目前沒有）'}"
        if query in names:
            return query, [], ""
        q = query.lower()
        matches = [s.name for s in sessions
                   if q in s.name.lower() or q == Path(s.path).name.lower() or q in Path(s.path).name.lower()]
        if len(matches) == 1:
            return matches[0], [], ""
        if matches:
            return None, matches, ""
        return None, [], f"找不到「{query}」。現有：{'、'.join(names) or '（目前沒有）'}"

    async def _with_name(self, query: str, action: str,
                         fn: Callable[[str], Awaitable[ToolResult]]) -> ToolResult:
        """先解析名稱再執行 fn；名稱有歧義就回傳 Choice 讓你選，找不到就回傳錯誤。"""
        name, candidates, error = await self.resolve(query)
        if name:
            return await fn(name)
        if candidates:
            return ToolResult(ok=False, choice=Choice(
                f"「{query}」符合好幾個 CLI，你要{action}的是哪一個？", candidates, fn))
        return ToolResult(ok=False, message=f"❌ {error}")

    @staticmethod
    def describe(command: str, state: str) -> str:
        """
        給人看的狀態：一般終端機（前景是 shell）、正在跑別的程式（例如 python），或 claude 的狀態。
        有了 shell/zuebot.zsh，你開的每個終端機視窗都會出現在清單裡，所以要分得出來。
        """
        if screen.is_shell(command):
            return "一般終端機（等你下指令）"
        if state == screen.UNKNOWN and command and "claude" not in command.lower() and command != "node":
            return f"正在執行 {command}"
        return screen.STATE_LABELS[state]

    def _check_text(self, text: str, source: str | None) -> str | None:
        """
        送出文字前的安全檢查。回傳 None 代表可以直接送；回傳字串代表需要你確認的原因。
          - source 不是 None 時（大腦決定的內容），必須是原話
          - 含有危險字眼（Phase 3：safety.find_danger）
        """
        reasons = []
        if source is not None and not is_verbatim(text, source):
            reasons.append("這段內容不是你的原話（大腦可能改寫了）")
        danger = safety.find_danger(text, self.cfg.extra_danger_words)
        if danger:
            reasons.append(f"內容含有可能危險的字眼：{'、'.join(danger)}")
        return "；".join(reasons) or None

    # ───────────── 查詢類工具 ─────────────
    async def list_clis(self, chat_id: int) -> ToolResult:
        """列出所有 CLI：名稱、工作目錄、狀態、是否關注中、是否為目前對象。"""
        sessions = await tmux_ops.list_sessions()
        if not sessions:
            return ToolResult(message="目前沒有 CLI 在跑。可以說「開一個新的 CLI 在 ~/projects/xxx」，或在電腦上用 cc 開。",
                              data=[])
        current = self.state.get_current(chat_id)
        rows, lines = [], []
        for s in sessions:
            try:
                state, _ = await cli.get_state(s.name, 30)
            except TmuxError:
                state = screen.UNKNOWN
            label = self.describe(s.command, state)
            watched = self.state.watcher(s.name) is not None
            rows.append({"name": s.name, "path": s.path, "state": label, "watched": watched,
                         "current": s.name == current})
            mark = "👉" if s.name == current else "▫️"
            lines.append(f"{mark} {s.name}{' 👀' if watched else ''}　{label}\n     {s.path}")
        return ToolResult(message="\n".join(lines) + "\n\n👉 目前對象　👀 關注中", data=rows)

    async def read_screen(self, query: str, lines: int = 60) -> ToolResult:
        """讀畫面（給大腦摘要用）。回傳的畫面已遮蔽機密。"""
        async def run(name: str) -> ToolResult:
            state, text = await cli.get_state(name, max(10, min(int(lines or 60), 300)))
            masked = safety.mask_secrets(screen.recent(text, 80))
            return ToolResult(data={"name": name, "state": screen.STATE_LABELS[state], "screen": masked})
        return await self._with_name(query, "看", run)

    async def show_raw(self, query: str, lines: int = 60) -> ToolResult:
        """給你看畫面原文（你說「給我原文」時用）。"""
        async def run(name: str) -> ToolResult:
            text = await tmux_ops.capture(name, max(10, min(int(lines or 60), 500)))
            return ToolResult(message=f"🖥 [{name}] 畫面原文：\n\n{safety.mask_secrets(text)}")
        return await self._with_name(query, "看原文", run)

    # ───────────── 會改變狀態的工具 ─────────────
    async def send_text(self, query: str, text: str, chat_id: int, source: str | None = None) -> ToolResult:
        """
        把文字送進 CLI，並自動關注，完成時回報。
        source：大腦決定要送的內容時，傳入你的原訊息，用來做「原話」檢查；你直接指定的內容傳 None。
        """
        if not (text or "").strip():
            return ToolResult(ok=False, message="❌ 沒有要送出的內容")

        async def run(name: str) -> ToolResult:
            async def do() -> str:
                note = await cli.deliver(name, text)
                self.state.watch(name, chat_id)
                self.awaiting[name] = time.time()
                return f"📨 已送到 [{name}]，完成時會通知你。{note}"
            reason = self._check_text(text, source)
            if screen.is_shell(await tmux_ops.pane_command(name)):
                # 一般終端機：送進去的文字會被 shell 當成指令執行，所以一律要你確認
                shell_note = "這是一般終端機（不是 claude），送出去的文字會被當成指令直接執行"
                reason = f"{shell_note}；{reason}" if reason else shell_note
            if reason:
                preview = text if len(text) <= 800 else text[:800] + "…（以下省略）"
                return ToolResult(pending=Pending(f"要把下面這段送進 [{name}] 嗎？\n原因：{reason}\n\n「{preview}」",
                                                  do, self.cfg.confirm_timeout))
            return ToolResult(message=await do())
        return await self._with_name(query, "送訊息", run)

    async def send_key(self, query: str, key: str, chat_id: int) -> ToolResult:
        """送一個按鍵。一律需要你確認，確認訊息會轉述畫面在問什麼。"""
        key = (key or "").strip().lower()
        if key not in tmux_ops.KEYS:
            return ToolResult(ok=False, message=f"❌ 不支援的按鍵「{key}」。可用：{' / '.join(tmux_ops.KEYS)}")

        async def run(name: str) -> ToolResult:
            state, text = await cli.get_state(name)
            tail = safety.mask_secrets(screen.recent(text, 12))
            signature = screen.dialog_signature(text)
            asking = (screen.PERMISSION, screen.MENU, screen.TRUST)

            async def do() -> str:
                # 你按同意時再看一次畫面：確認畫面換成另一個了，或原本沒有確認畫面、現在卻跳出來了 → 不送
                now_state, now_text = await cli.get_state(name)
                if state in asking and (now_state != state or screen.dialog_signature(now_text) != signature):
                    return f"[{name}] 的畫面已經跟你確認時不一樣了，為了安全沒有送出按鍵。要的話請再說一次。"
                if state not in asking and now_state in asking:
                    return f"[{name}] 剛剛跳出了新的確認畫面，為了安全沒有送出按鍵。要的話請再說一次。"
                await tmux_ops.send_key(name, key)
                self.state.watch(name, chat_id)
                await asyncio.sleep(1.5)                 # 等畫面更新，讓你確認按對了
                after = safety.mask_secrets(screen.recent(await tmux_ops.capture(name, 30), 10))
                return f"⌨️ 已對 [{name}] 按下 {KEY_LABELS.get(key, key)}\n\n{after}"
            title = (f"要對 [{name}] 按下 {KEY_LABELS.get(key, key)} 嗎？\n"
                     f"它現在{screen.STATE_LABELS[state]}，畫面最後幾行：\n\n{tail}")
            return ToolResult(pending=Pending(title, do, self.cfg.confirm_timeout))
        return await self._with_name(query, "按鍵", run)

    async def watch(self, query: str, chat_id: int) -> ToolResult:
        """開始關注：完成或需要你確認時通知。"""
        async def run(name: str) -> ToolResult:
            self.state.watch(name, chat_id)
            state, _ = await cli.get_state(name)
            if state == screen.BUSY:
                self.awaiting.setdefault(name, time.time())
            return ToolResult(message=f"👀 開始關注 [{name}]（目前{screen.STATE_LABELS[state]}），完成或需要你確認時會通知你。")
        return await self._with_name(query, "關注", run)

    async def unwatch(self, query: str) -> ToolResult:
        """取消關注。已經不存在的 session 也可以取消。"""
        if query in self.state.watches:
            self.state.unwatch(query)
            return ToolResult(message=f"🙈 已停止關注 [{query}]")

        async def run(name: str) -> ToolResult:
            if self.state.unwatch(name):
                self.awaiting.pop(name, None)
                return ToolResult(message=f"🙈 已停止關注 [{name}]")
            return ToolResult(message=f"[{name}] 本來就沒有在關注")
        return await self._with_name(query, "取消關注", run)

    async def set_current(self, query: str, chat_id: int) -> ToolResult:
        """設定目前對象（之後以 > 開頭的訊息會直接送進去）。"""
        async def run(name: str) -> ToolResult:
            self.state.set_current(chat_id, name)
            return ToolResult(message=f"👉 目前對象：{name}。之後以 > 開頭的訊息會原文送進去。")
        return await self._with_name(query, "設為目前對象", run)

    async def arm_paste(self, query: str, chat_id: int) -> ToolResult:
        """準備把你的「下一則訊息」原封不動貼進 CLI。"""
        async def run(name: str) -> ToolResult:
            deadline = time.time() + PASTE_TIMEOUT
            self.paste_armed[chat_id] = (name, deadline)
            self.spawn(self._paste_reminder(chat_id, name, deadline))
            return ToolResult(message=f"📋 好，你的下一則訊息會原封不動貼進 [{name}]（5 分鐘內有效，傳 /cancel 取消）。")
        return await self._with_name(query, "貼上", run)

    def take_paste(self, chat_id: int) -> tuple[str | None, bool]:
        """取出「等待貼上」的目標，回傳 (session 名稱, 是否已逾時)。沒有在等待就回傳 (None, False)。"""
        armed = self.paste_armed.pop(chat_id, None)
        if not armed:
            return None, False
        name, deadline = armed
        return (name, False) if time.time() < deadline else (None, True)

    async def _paste_reminder(self, chat_id: int, name: str, deadline: float) -> None:
        """/paste 逾時提醒：時間到了還沒收到內容，就通知你一聲（Phase 4）。"""
        await asyncio.sleep(max(0, deadline - time.time()))
        if self.paste_armed.get(chat_id) == (name, deadline):
            self.paste_armed.pop(chat_id, None)
            await self.ui.notify(chat_id, f"⏰ 「貼進 [{name}]」已經過了 5 分鐘，已取消。要貼的話請重新說一次。")

    def _resolve_folder(self, folder: str) -> tuple[Path | None, str]:
        """
        把資料夾轉成絕對路徑並檢查是否在 ALLOWED_ROOT 底下。
        回傳 (路徑, 錯誤訊息)；路徑在範圍內但不存在時，回傳路徑和空字串（呼叫端決定要不要建立）。
        """
        try:
            path = Path(folder.strip()).expanduser()
            if not path.is_absolute():
                path = self.cfg.allowed_root / path
            path = path.resolve()
        except (OSError, RuntimeError, ValueError):
            return None, f"看不懂這個路徑：{folder}"
        root = self.cfg.allowed_root
        if path != root and root not in path.parents:
            return None, f"只能在 {root} 底下開 CLI，「{path}」不在範圍內"
        if path.exists() and not path.is_dir():
            return None, f"「{path}」是檔案，不是資料夾"
        return path, ""

    async def new_cli(self, name: str, folder: str, first_prompt: str, chat_id: int,
                      source: str | None = None) -> ToolResult:
        """
        開新的 tmux session 並啟動 claude；啟動完成後（處理完信任提示）送出第一個任務。
        資料夾不存在、第一個任務需要確認時，整個動作會變成 Pending 等你同意。
        """
        cwd, error = self._resolve_folder(folder or "")
        if cwd is None:
            return ToolResult(ok=False, message=f"❌ {error}")
        name = (name or "").strip() or safe_session_name(cwd.name)
        try:
            tmux_ops.validate_name(name)
        except TmuxError:
            name = safe_session_name(name)
            if not name:
                return ToolResult(ok=False, message="❌ 名稱只能用中英文、數字、底線、連字號")
        if await tmux_ops.session_exists(name):
            return ToolResult(ok=False, message=f"❌ 已經有 [{name}] 了，換個名稱，或直接跟它說話")
        first_prompt = (first_prompt or "").strip()

        async def do() -> str:
            cwd.mkdir(parents=True, exist_ok=True)
            await tmux_ops.new_session(name, str(cwd), cli.claude_command(self.cfg.claude_cmd))
            self.state.set_current(chat_id, name)
            self.state.watch(name, chat_id)
            self.spawn(self._startup(name, chat_id, first_prompt, cwd))
            return f"🆕 已開 [{name}]（{cwd}），設為目前對象，等待 claude 啟動…"

        reasons = []
        if not cwd.exists():
            reasons.append(f"資料夾 {cwd} 不存在，會幫你建立")
        if first_prompt:
            check = self._check_text(first_prompt, source)
            if check:
                reasons.append(f"第一個任務：{check}")
        if reasons:
            title = (f"要開新的 CLI [{name}] 嗎？\n資料夾：{cwd}\n"
                     + (f"第一個任務：「{first_prompt}」\n" if first_prompt else "")
                     + "需要確認的原因：" + "；".join(reasons))
            return ToolResult(pending=Pending(title, do, self.cfg.confirm_timeout))
        return ToolResult(message=await do())

    async def _startup(self, name: str, chat_id: int, first_prompt: str, cwd: Path, trusted: bool = False) -> None:
        """背景工作：等新 CLI 啟動。遇到信任提示就用按鈕問你；就緒後送出第一個任務。"""
        try:
            state, text = await cli.wait_for_startup(name)
        except TmuxError as e:
            await self.ui.notify(chat_id, f"❌ {e}")
            return
        if state == screen.IDLE:
            if not first_prompt:
                await self.ui.notify(chat_id, f"✅ [{name}] 已就緒，可以開始交代工作了。")
                return
            try:
                note = await cli.deliver(name, first_prompt)
            except TmuxError as e:
                await self.ui.notify(chat_id, f"❌ 送出第一個任務失敗：{e}")
                return
            self.awaiting[name] = time.time()
            await self.ui.notify(chat_id, f"📨 [{name}] 已就緒，第一個任務已送出，完成時會通知你。{note}")
        elif state == screen.TRUST and not trusted:
            async def accept() -> str:
                await cli.accept_trust(name)
                self.spawn(self._startup(name, chat_id, first_prompt, cwd, trusted=True))
                return f"✅ 已信任 {cwd}，繼續啟動 [{name}]…"
            tail = safety.mask_secrets(screen.recent(text, 12))
            await self.ui.confirm(chat_id, Pending(
                f"[{name}] 是第一次在 {cwd} 開 claude，它在問你是否信任這個資料夾（信任後 claude 才能讀寫裡面的檔案）。\n\n"
                f"{tail}\n\n要信任並繼續嗎？", accept, self.cfg.confirm_timeout))
        elif state == screen.LOGIN:
            await self.ui.notify(chat_id, f"❌ [{name}] 的 claude 還沒登入，請回到電腦上執行 claude 並輸入 /login。")
        else:
            tail = safety.mask_secrets(screen.recent(text, 12))
            await self.ui.notify(chat_id, f"⚠️ [{name}] 等了一陣子還沒看到輸入框（{screen.STATE_LABELS[state]}），畫面：\n\n{tail}")

    async def kill_cli(self, query: str, chat_id: int) -> ToolResult:
        """關閉 CLI（裡面的 claude 會被結束）。一律需要你確認。"""
        async def run(name: str) -> ToolResult:
            async def do() -> str:
                await tmux_ops.kill_session(name)
                self.state.forget_session(name)
                self.awaiting.pop(name, None)
                return f"🗑 已關閉 [{name}]"
            info = next((s for s in await tmux_ops.list_sessions() if s.name == name), None)
            where = f"（{info.path}）" if info else ""
            return ToolResult(pending=Pending(f"要關閉 [{name}]{where} 嗎？裡面的 claude 會被結束，正在做的事會中斷。",
                                              do, self.cfg.confirm_timeout))
        return await self._with_name(query, "關閉", run)

    # ───────────── 給大腦用的統一入口 ─────────────
    async def call(self, action: dict[str, Any], chat_id: int, source: str) -> ToolResult:
        """
        執行大腦決定的一個動作。action 例如 {"tool": "send_text", "name": "競賽", "text": "…"}。
        每個欄位都重新驗證型別；未知的工具直接拒絕。source 是你的原訊息（給原話檢查用）。
        """
        tool = str(action.get("tool", ""))
        name = str(action.get("name") or "")
        text = str(action.get("text") or "")
        try:
            lines = int(action.get("lines") or 60)
        except (TypeError, ValueError):
            lines = 60
        try:
            if tool == "list_clis":
                return await self.list_clis(chat_id)
            if tool == "read_screen":
                return await self.read_screen(name, lines)
            if tool == "show_raw":
                return await self.show_raw(name, lines)
            if tool == "send_text":
                return await self.send_text(name, text, chat_id, source=source)
            if tool == "send_key":
                return await self.send_key(name, str(action.get("key") or ""), chat_id)
            if tool == "watch":
                return await self.watch(name, chat_id)
            if tool == "unwatch":
                return await self.unwatch(name)
            if tool == "set_current":
                return await self.set_current(name, chat_id)
            if tool == "arm_paste":
                return await self.arm_paste(name, chat_id)
            if tool == "new_cli":
                return await self.new_cli(name, str(action.get("cwd") or ""), str(action.get("first_prompt") or ""),
                                          chat_id, source=source)
            if tool == "kill_cli":
                return await self.kill_cli(name, chat_id)
            if tool == "ask_choice":
                return await self._ask_choice(action, chat_id, source)
        except TmuxError as e:
            return ToolResult(ok=False, message=f"❌ {e}")
        return ToolResult(ok=False, message=f"❌ 大腦要求了不存在的工具「{tool}」，已忽略")

    async def _ask_choice(self, action: dict[str, Any], chat_id: int, source: str) -> ToolResult:
        """大腦覺得指稱有歧義時：列出候選讓你選，選好後執行 then_tool。候選只保留真的存在的 CLI。"""
        existing = {s.name for s in await tmux_ops.list_sessions()}
        options = [str(c) for c in action.get("candidates") or [] if str(c) in existing]
        then_tool = str(action.get("then_tool") or "watch")
        if then_tool in ("ask_choice", ""):
            then_tool = "watch"
        if not options:
            return ToolResult(ok=False, message="❌ 找不到符合的 CLI，請說得更明確一點")

        async def run(chosen: str) -> ToolResult:
            return await self.call({**action, "tool": then_tool, "name": chosen}, chat_id, source)
        question = str(action.get("question") or "你指的是哪一個？")
        return ToolResult(choice=Choice(question, options, run))
