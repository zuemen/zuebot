"""
cli.py —— 「懂 Claude Code 畫面」的高階操作

分層：
  tmux_ops.py  只會下 tmux 指令（貼上、按鍵、讀畫面）
  screen.py    只會看畫面文字判斷狀態
  cli.py       把兩者組合起來：送字前先看狀態、送完確認真的送出、開新 CLI 時等它啟動完成
  tools.py     再往上一層：驗證參數、需要你確認的動作（給 bot 和大腦用）

這裡處理 PROMPT.md Phase 1 提到的 TUI 問題：
  - 已知 Claude Code 的問題：Enter 有時被吃掉，文字停在輸入框沒送出
    → 送完等一下再讀畫面，文字還在輸入框就補按一次 Enter（最多補兩次）。
  - CLI 正在顯示確認選單時貼字，文字會被當成選單按鍵 → 先檢查狀態，不是可輸入的狀態就拒絕。
  - 第一次在某資料夾開 claude 會問是否信任 → wait_for_startup 會偵測出來，交給呼叫端問你。
"""

from __future__ import annotations

import asyncio
import logging

from . import screen, tmux_ops
from .tmux_ops import TmuxError

log = logging.getLogger(__name__)

VERIFY_DELAY = 1.5        # 送出後等幾秒再檢查輸入框
MAX_EXTRA_ENTERS = 2      # 文字還卡在輸入框時，最多補按幾次 Enter
STARTUP_TIMEOUT = 45      # 開新 CLI 時最多等幾秒讓 claude 啟動
STARTUP_POLL = 1.0        # 啟動期間每幾秒看一次畫面


# Claude Code 會在它開出來的程式裡設定這些環境變數；新版偵測到 CLAUDECODE 會以為自己被開在另一個
# Claude Code 裡而拒絕啟動。你在 Claude Code 裡執行 selftest 或 bot 時，開出來的 CLI 會繼承它們，所以要清掉。
NESTED_ENV_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")


def claude_command(base: str) -> str:
    """組出在 tmux 裡啟動 claude 的指令：先用 env -u 清掉會被誤判成「巢狀執行」的變數。"""
    unset = " ".join(f"-u {name}" for name in NESTED_ENV_VARS)
    return f"env {unset} {base}"


async def get_state(name: str, lines: int = 40) -> tuple[str, str]:
    """讀畫面並判斷狀態，回傳 (狀態, 畫面文字)。"""
    text = await tmux_ops.capture(name, lines)
    state = screen.detect_state(text)
    return state, text


async def deliver(name: str, text: str, verify: bool = True) -> str:
    """
    把文字送進 CLI 並確認真的送出了。回傳一句補充說明（通常是空字串）。

    會拒絕送出的情況（丟出 TmuxError）：
      - CLI 正在問信任資料夾或權限確認：這時打字會被當成選單操作
      - claude 還沒登入
    """
    state, _ = await get_state(name)
    if state in (screen.TRUST, screen.PERMISSION):
        raise TmuxError(f"[{name}] 現在{screen.STATE_LABELS[state]}，要先處理那個畫面才能送訊息")
    if state == screen.LOGIN:
        raise TmuxError(f"[{name}] 的 claude 還沒登入，請先在電腦上登入")

    await tmux_ops.paste_text(name, text)
    if not verify:
        return ""
    for attempt in range(MAX_EXTRA_ENTERS):
        await asyncio.sleep(VERIFY_DELAY)
        after = await tmux_ops.capture(name, 40)
        # 安全規則：只有畫面是「閒置等輸入」時才可以補按 Enter。
        # 如果畫面已經變成確認選單，這時按 Enter 等於幫你按了「允許」，絕對不行。
        if screen.detect_state(after) != screen.IDLE or not screen.text_still_in_input(after, text):
            return "" if attempt == 0 else "（Enter 第一次沒生效，已自動補按）"
        log.info("[%s] 文字還停在輸入框，補按 Enter（第 %d 次）", name, attempt + 1)
        await tmux_ops.send_key(name, "enter")
    await asyncio.sleep(VERIFY_DELAY)
    final = await tmux_ops.capture(name, 40)
    if screen.text_still_in_input(final, text):
        return "⚠️ 文字好像還停在輸入框裡沒送出，請用 /look 看一下畫面"
    return "（Enter 第一次沒生效，已自動補按）"


async def wait_for_startup(name: str, timeout: float = STARTUP_TIMEOUT) -> tuple[str, str]:
    """
    開新 CLI 後等待 claude 啟動，直到出現「可以處理」的狀態才回傳 (狀態, 畫面)：
      IDLE（可以送任務）、TRUST（要問你是否信任）、LOGIN（沒登入）、PERMISSION。
    逾時回傳最後看到的狀態（通常是 UNKNOWN）。session 在等待中消失時丟出 TmuxError。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    state, text = screen.UNKNOWN, ""
    while loop.time() < deadline:
        if not await tmux_ops.session_exists(name):
            raise TmuxError(f"[{name}] 啟動後馬上就結束了，可能是 claude 指令找不到（檢查 .env 的 CLAUDE_CMD）")
        state, text = await get_state(name)
        if state in (screen.IDLE, screen.TRUST, screen.LOGIN, screen.PERMISSION):
            return state, text
        await asyncio.sleep(STARTUP_POLL)
    return state, text


async def accept_trust(name: str) -> None:
    """在信任資料夾的提示上按 Enter（選第一個選項「是，信任」）。只有在畫面真的是信任提示時才按。"""
    state, _ = await get_state(name)
    if state != screen.TRUST:
        raise TmuxError(f"[{name}] 現在不是信任資料夾的畫面，沒有按任何鍵")
    await tmux_ops.send_key(name, "enter")
