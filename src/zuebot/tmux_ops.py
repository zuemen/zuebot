"""
tmux_ops.py —— 所有 tmux 操作都集中在這裡（全專案唯一呼叫 tmux 的地方）

為什麼要集中？
  之後要支援「一個中央 bot 控制好幾台電腦」（PROMPT.md Phase 5）時，
  只要把這裡的「執行器」從本機換成 `ssh 機器 tmux …`，其他模組完全不用改。

v0 踩過、這裡特別處理的三個坑（都在 tmux 3.4 實測過）：
  1. 名稱前綴比對：`-t demo` 在 demo 不存在時會比對到 demo2，可能關錯或送錯 CLI。
     → session 指令一律用 `=名稱`（精確比對），pane 指令一律用 `=名稱:`（一定要有冒號，
       否則 tmux 會把 `=名稱` 當成字面上的 pane 名稱而找不到）。
  2. 中文名稱：環境沒設 UTF-8 locale 時（launchd、ssh 常見），tmux 會把「競賽」變成「__」。
     → 每個指令都加 `-u`，並補上 UTF-8 的 LC_CTYPE。
  3. v0 在 async 程式裡用同步的 subprocess.run 和 time.sleep，tmux 一慢整個 bot 就卡住。
     → 這裡全部改成 asyncio 子行程，有逾時保護。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import sys
import uuid
from dataclasses import dataclass

log = logging.getLogger(__name__)

COMMAND_TIMEOUT = 10        # 單一 tmux 指令最多等幾秒
PASTE_ENTER_DELAY = 0.5     # 貼上後等多久才按 Enter。不能同時送：Claude Code 有已知問題，
                            # 貼上和按鍵擠在同一次輸入裡時，貼上的文字會被丟掉。

# 可以送的按鍵：使用者輸入的名稱 → tmux send-keys 的按鍵名稱
KEYS = {
    "enter": "Enter", "esc": "Escape", "up": "Up", "down": "Down", "tab": "Tab",
    "ctrl-c": "C-c", "1": "1", "2": "2", "3": "3", "y": "y", "n": "n",
}

# session 名稱規則：中英數、底線、連字號，1～30 字。
# 不允許 `:` 和 `.`，因為它們在 tmux 的目標語法裡有特殊意義（session:window.pane）。
NAME_RE = re.compile(r"^[\w\-]{1,30}$")
PANE_ID_RE = re.compile(r"^%\d+$")   # tmux pane id 的格式，例如 %3


class TmuxError(Exception):
    """tmux 操作失敗。訊息是給人看的中文說明，bot 可以直接轉給使用者。"""


@dataclass
class TmuxResult:
    """一個 tmux 指令的執行結果。"""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """結束碼為 0 代表成功。"""
        return self.returncode == 0


@dataclass
class SessionInfo:
    """一個 tmux session（也就是一個被管理的 CLI）的基本資訊。"""

    name: str       # session 名稱，例如「競賽」
    path: str       # 目前作用中 pane 的工作目錄
    command: str    # 目前前景程式，例如 claude、node、zsh
    pane_id: str    # 目前作用中 pane 的 id，例如 %3


def _utf8_env() -> dict[str, str]:
    """
    複製目前的環境變數，必要時補上 UTF-8 locale。

    為什麼：launchd 啟動的程式通常沒有 LANG，tmux 會把非 ASCII 的 session 名稱變成底線，
    而且由 bot 開的新 session 會繼承這份環境，裡面的 claude 也需要 UTF-8 才能正確處理中文。
    """
    env = dict(os.environ)
    current = (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or "").upper()
    if "UTF-8" not in current and "UTF8" not in current:
        env.pop("LC_ALL", None)   # LC_ALL 優先權最高，如果它是非 UTF-8 的值會蓋掉 LC_CTYPE
        env["LC_CTYPE"] = "UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    return env


class LocalRunner:
    """
    在本機執行 tmux 的「執行器」。

    Phase 5 會再寫一個 SSHRunner（在指令前面加上 ssh 機器），介面跟這個一模一樣：
    只有一個 async run() 方法。其他函式只透過 _runner.run() 呼叫 tmux。
    """

    def __init__(self, tmux_bin: str = "tmux") -> None:
        """記住 tmux 執行檔位置，並準備好 UTF-8 環境變數。"""
        self.tmux_bin = tmux_bin
        self.env = _utf8_env()

    async def run(self, args: list[str], input_text: str | None = None,
                  timeout: float = COMMAND_TIMEOUT) -> TmuxResult:
        """
        執行一個 tmux 指令並回傳結果。不丟例外：找不到 tmux、逾時都轉成 TmuxResult，
        由呼叫端看 returncode 決定怎麼處理。
        """
        cmd = [self.tmux_bin, "-u", *args]
        log.debug("tmux ▶ %s", shlex.join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
        except FileNotFoundError:
            return TmuxResult(127, "", f"找不到 tmux 執行檔「{self.tmux_bin}」，請確認已安裝，或在 .env 設定 TMUX_BIN")
        except OSError as e:
            return TmuxResult(126, "", f"無法執行 tmux：{e}")

        data = input_text.encode("utf-8") if input_text is not None else None
        try:
            out, err = await asyncio.wait_for(proc.communicate(data), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return TmuxResult(124, "", f"tmux 指令超過 {timeout} 秒沒有回應")

        result = TmuxResult(proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
        log.debug("tmux ◀ rc=%s stderr=%r", result.returncode, result.stderr.strip()[:200])
        return result


# 目前使用的執行器。configure() 可以換掉它（例如換 tmux 路徑，或測試時指定別的 socket）。
_runner = LocalRunner()


def configure(tmux_bin: str = "tmux") -> None:
    """設定 tmux 執行檔位置（bot 啟動時依 .env 的 TMUX_BIN 呼叫一次）。"""
    global _runner
    _runner = LocalRunner(tmux_bin)


def session_target(name: str) -> str:
    """給 session 類指令用的精確目標（has-session、kill-session、attach）。"""
    return f"={name}"


def pane_target(name: str) -> str:
    """給 pane 類指令用的精確目標（capture-pane、send-keys、paste-buffer）：那個 session 目前的 pane。"""
    return f"={name}:"


def validate_name(name: str) -> None:
    """檢查 session 名稱合不合規則，不合就丟出 TmuxError。"""
    if not NAME_RE.match(name or ""):
        raise TmuxError("名稱只能用中英文、數字、底線、連字號，30 字以內（不能有空白、冒號、句點）")


async def list_sessions() -> list[SessionInfo]:
    """列出所有 tmux session。沒有 tmux server 在跑（一個 session 都沒有）時回傳空清單。"""
    fmt = "#{session_name}\t#{pane_current_path}\t#{pane_current_command}\t#{pane_id}"
    r = await _runner.run(["list-sessions", "-F", fmt])
    if not r.ok:
        # 「no server running」或「error connecting」是正常的「目前沒有 session」，其他錯誤記到 log
        if "no server running" not in r.stderr and "error connecting" not in r.stderr:
            log.warning("list-sessions 失敗：%s", r.stderr.strip())
        return []
    sessions = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        name, path, cmd, pane = (line.split("\t") + ["", "", "", ""])[:4]
        sessions.append(SessionInfo(name=name, path=path, command=cmd, pane_id=pane))
    return sessions


async def session_exists(name: str) -> bool:
    """這個名稱的 session 是否存在（精確比對，不會把 demo 當成 demo2）。"""
    if not NAME_RE.match(name or ""):
        return False
    r = await _runner.run(["has-session", "-t", session_target(name)])
    return r.ok


async def session_of_pane(pane_id: str) -> str | None:
    """
    用 pane id（例如 %3，hook 從 TMUX_PANE 拿到的）反查它屬於哪個 session。
    pane 已經不存在時回傳 None。

    為什麼由 bot 反查而不是 hook 自己查：hook 執行時的環境不一定有 UTF-8，查出來的中文名稱
    可能變成底線；而且規定只有本模組能呼叫 tmux。
    """
    if not PANE_ID_RE.match(pane_id or ""):
        return None
    r = await _runner.run(["display-message", "-p", "-t", pane_id, "#{session_name}"])
    name = r.stdout.strip()
    return name if r.ok and name else None


async def capture(name: str, lines: int = 40) -> str:
    """
    讀取 CLI 畫面最後 N 行（包含往上捲的歷史）。
    -J 會把因為視窗太窄而被自動換行的長行接回來。
    """
    lines = max(1, min(int(lines), 2000))
    r = await _runner.run(["capture-pane", "-p", "-J", "-t", pane_target(name), "-S", f"-{lines}"])
    if not r.ok:
        raise TmuxError(f"讀不到 [{name}] 的畫面：{r.stderr.strip() or '未知錯誤'}")
    # 歷史不足 N 行時，畫面下方的空白行也會被抓進來，去掉尾端空白讓訊息乾淨
    return r.stdout.rstrip()


async def tail(name: str, lines: int = 15) -> str:
    """只取畫面最後 N 個「非空白尾端」的行，用在通知訊息裡。"""
    return "\n".join((await capture(name, lines)).splitlines()[-lines:])


async def paste_text(name: str, text: str) -> None:
    """
    把一段文字貼進 CLI 的輸入框，再按 Enter 送出。

    為什麼用 paste-buffer -p 而不是 send-keys：
      -p 會在 CLI 有開啟「bracketed paste」時，用特殊標記把整段包起來，
      Claude Code 就知道這是一次貼上，多行文字不會在第一個換行就被送出。
    每次用不同的 buffer 名稱，避免兩個訊息同時送時互相蓋掉。
    """
    if not text:
        raise TmuxError("沒有內容可以送")
    buf = f"zuebot-{uuid.uuid4().hex[:8]}"
    r = await _runner.run(["load-buffer", "-b", buf, "-"], input_text=text)
    if not r.ok:
        raise TmuxError(f"準備貼上內容失敗：{r.stderr.strip()}")
    r = await _runner.run(["paste-buffer", "-p", "-d", "-b", buf, "-t", pane_target(name)])
    if not r.ok:
        await _runner.run(["delete-buffer", "-b", buf])   # 貼失敗時 -d 不會生效，自己清掉
        raise TmuxError(f"貼到 [{name}] 失敗：{r.stderr.strip()}")
    await asyncio.sleep(PASTE_ENTER_DELAY)   # 非同步等待，不會卡住 bot
    r = await _runner.run(["send-keys", "-t", pane_target(name), "Enter"])
    if not r.ok:
        raise TmuxError(f"已貼上，但按 Enter 失敗：{r.stderr.strip()}")


async def send_key(name: str, key: str) -> None:
    """送出一個按鍵（只接受 KEYS 裡列出的按鍵，避免送出任意控制序列）。"""
    tmux_key = KEYS.get(key.lower())
    if tmux_key is None:
        raise TmuxError(f"不支援的按鍵「{key}」。可用：{' / '.join(KEYS)}")
    r = await _runner.run(["send-keys", "-t", pane_target(name), tmux_key])
    if not r.ok:
        raise TmuxError(f"對 [{name}] 送出按鍵失敗：{r.stderr.strip()}")


async def new_session(name: str, cwd: str, command: str) -> None:
    """
    在背景開一個新的 tmux session，在 cwd 資料夾執行 command（通常是 claude）。
    路徑是否在允許範圍內由呼叫端檢查；這裡只負責 tmux 本身。
    """
    validate_name(name)
    if await session_exists(name):
        raise TmuxError(f"已經有 [{name}] 了")
    r = await _runner.run(["new-session", "-d", "-s", name, "-c", cwd, command])
    if not r.ok:
        raise TmuxError(f"開啟 [{name}] 失敗：{r.stderr.strip()}")


async def kill_session(name: str) -> None:
    """關閉一個 session（裡面的 CLI 會跟著結束）。"""
    r = await _runner.run(["kill-session", "-t", session_target(name)])
    if not r.ok:
        raise TmuxError(f"關閉 [{name}] 失敗：{r.stderr.strip()}")
