"""
screen.py —— 判讀 Claude Code 的終端畫面（純文字處理，不碰 tmux）

bot 需要知道 CLI 現在是什麼狀態，才能決定能不能送字、要不要問你：
  TRUST       第一次在某資料夾開 claude，問「是否信任這個資料夾」
  PERMISSION  權限確認選單（例如「Do you want to proceed? 1. Yes 2. … 3. No」）
  MENU        其他選單（例如 claude 問你要選哪個方案：1. … 2. …）——這時打字會被當成選擇
  LOGIN       claude 還沒登入
  BUSY        正在工作（畫面上有「esc to interrupt」）
  IDLE        閒置，輸入框在等你打字
  UNKNOWN     看不出來（例如還在啟動中）

為什麼用「關鍵字 + 寬鬆的正規表示式」：
  Claude Code 的介面文字會隨版本調整，官方也沒有文件化。這裡盡量涵蓋已知的寫法，
  Phase 1 在 Mac 上跑 `python -m zuebot.selftest` 時會把實際畫面記錄下來，必要時再調整。
"""

from __future__ import annotations

import re

TRUST = "trust"
PERMISSION = "permission"
MENU = "menu"
LOGIN = "login"
BUSY = "busy"
IDLE = "idle"
UNKNOWN = "unknown"

# 給人看的狀態說明
STATE_LABELS = {
    TRUST: "在問是否信任資料夾",
    PERMISSION: "在等你確認權限",
    MENU: "在等你從選單選一個選項",
    LOGIN: "還沒登入 claude",
    BUSY: "執行中",
    IDLE: "閒置（等你輸入）",
    UNKNOWN: "狀態不明",
}

SCAN_LINES = 30   # 只看畫面最後幾行：上面的歷史輸出可能剛好含有這些關鍵字

_TRUST_RE = re.compile(
    r"do you trust|trust (the files in )?this (folder|project|workspace|directory)|one you trust"
    r"|yes,? i trust|quick safety check|信任",
    re.IGNORECASE,
)
_PERMISSION_ASK_RE = re.compile(
    r"do you want to|would you like to|allow (this|claude)|permission",
    re.IGNORECASE,
)
_PERMISSION_OPTION_RE = re.compile(r"(^|\s)1\.\s*(yes|allow)", re.IGNORECASE | re.MULTILINE)
_LOGIN_RE = re.compile(
    r"select login method|please run /login|run /login|not logged in|invalid api key|login required",
    re.IGNORECASE,
)
_BUSY_RE = re.compile(r"(esc|ctrl\+c) to interrupt", re.IGNORECASE)
# 輸入框那一行：可能有外框「│」，提示符號是「>」或「❯」
_PROMPT_LINE_RE = re.compile(r"^\s*(?:[│|]\s*)?[>❯](?:\s(.*?))?\s*(?:[│|])?\s*$")
_IDLE_HINT_RE = re.compile(r"\? for shortcuts|shift\+tab to cycle", re.IGNORECASE)
_MENU_LINE_RE = re.compile(r"^\s*(?:[│|]\s*)?[>❯]\s*\d+\.\s")   # 「❯ 1. Yes」是選單，不是輸入框
_OPTION_LINE_RE = re.compile(r"^\s*(?:[│|]\s*)?(?:[>❯]\s*)?[1-9]\.\s+\S")   # 任何「1. 選項」形式的行
_SELECT_HINT_RE = re.compile(r"enter to select|↑/↓ to navigate|esc to cancel", re.IGNORECASE)

SHELLS = {"zsh", "bash", "sh", "fish", "-zsh", "-bash", "login", "tcsh", "dash"}


def recent(screen: str, lines: int = SCAN_LINES) -> str:
    """取畫面最後 N 個非空白行（tmux 抓下來的畫面底部常有很多空行）。"""
    rows = [r for r in screen.splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


def _last_index(rows: list[str], pattern: re.Pattern) -> int:
    """回傳最後一個符合 pattern 的行號；都不符合回傳 -1。"""
    for i in range(len(rows) - 1, -1, -1):
        if pattern.search(rows[i]):
            return i
    return -1


def _prompt_index(rows: list[str]) -> int:
    """回傳最後一個「輸入框」行的行號（排除「❯ 1. Yes」這種選單行）；沒有回傳 -1。"""
    for i in range(len(rows) - 1, -1, -1):
        if not _MENU_LINE_RE.match(rows[i]) and _PROMPT_LINE_RE.match(rows[i]):
            return i
    return -1


def detect_state(screen: str) -> str:
    """
    判斷畫面目前的狀態，回傳 TRUST / PERMISSION / LOGIN / BUSY / IDLE / UNKNOWN 其中之一。

    判斷原則：「越下面越新」。畫面上可能殘留之前的選單或訊息，
    所以比較各種標記出現的位置，最靠近底部的那個才代表現在的狀態。
    """
    rows = [r for r in screen.splitlines() if r.strip()][-SCAN_LINES:]
    if not rows:
        return UNKNOWN
    prompt = _prompt_index(rows)
    option = _last_index(rows, _PERMISSION_OPTION_RE)
    confirm_hint = _last_index(rows, re.compile(r"proceed|enter to confirm", re.IGNORECASE))

    # 信任提示：要同時看到「信任」的字眼和選項，而且選項在輸入框下面（＝目前顯示中）
    trust = _last_index(rows, _TRUST_RE)
    trust_option = max(option, confirm_hint)
    if trust >= 0 and trust_option >= trust and trust_option > prompt:
        return TRUST
    # 權限確認：問句 + 「1. Yes」選項，而且選項在輸入框下面
    ask = _last_index(rows, _PERMISSION_ASK_RE)
    if ask >= 0 and option >= 0 and option > prompt and option >= ask - 1:
        return PERMISSION
    # 其他選單：輸入框下面出現「1. …」選項，或出現「Enter to select」提示 → 打字會被當成選擇
    menu_option = _last_index(rows, _OPTION_LINE_RE)
    select_hint = _last_index(rows, _SELECT_HINT_RE)
    if (menu_option > prompt and menu_option >= len(rows) - 10) or (select_hint > prompt and select_hint >= 0):
        return MENU
    login = _last_index(rows, _LOGIN_RE)
    if login >= 0 and login > prompt:
        return LOGIN
    busy = _last_index(rows, _BUSY_RE)
    if busy >= 0 and busy >= len(rows) - 8:     # 執行中的提示就在輸入框正上方
        return BUSY
    if prompt >= 0 or _IDLE_HINT_RE.search("\n".join(rows[-5:])):
        return IDLE
    return UNKNOWN


def input_box_line(screen: str) -> str | None:
    """
    找出「目前作用中」的輸入框，回傳框內的文字（空的輸入框回傳 ""）；找不到回傳 None。

    為了不被畫面上殘留的舊輸入框騙到，有兩個條件：
      1. 輸入框必須在畫面最後 6 行之內（真的輸入框底下只會有外框和提示列）
      2. 輸入框下面不能有選單選項（有選單代表現在是確認畫面，不是輸入框）
    """
    rows = [r for r in screen.splitlines() if r.strip()][-15:]
    idx = _prompt_index(rows)
    if idx < 0 or idx < len(rows) - 6:
        return None
    if any(_MENU_LINE_RE.match(r) or _PERMISSION_OPTION_RE.search(r) for r in rows[idx + 1:]):
        return None
    m = _PROMPT_LINE_RE.match(rows[idx])
    return (m.group(1) or "").strip() if m else None


def text_still_in_input(screen: str, text: str) -> bool:
    """
    送出後檢查：我們送的文字是不是還停在輸入框裡（代表 Enter 被吃掉、沒送出）。

    比對方式：輸入框裡出現「[Pasted text」（長文字貼上後的摺疊顯示），
    或出現我們文字第一行的開頭。輸入框的灰色提示文字（例如 Try "…"）不會被誤判，因為內容不同。
    """
    box = input_box_line(screen)
    if not box:
        return False
    if "[Pasted text" in box:
        return True
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return bool(first) and first[:12] in box


_MENU_OPTION_RE = re.compile(r"^\s*(?:[│|]\s*)?(?:[❯>]\s*)?([1-9])\.\s+(.+?)\s*(?:[│|]\s*)?$")


def menu_options(screen_text: str) -> list[tuple[str, str]]:
    """
    讀出畫面最下方選單的選項，例如 [("1", "Yes"), ("2", "Yes, and don't ask again …"), ("3", "No …")]。
    用來產生 Telegram 上的按鈕，讓按鈕文字跟電腦上看到的一樣。讀不到就回傳空清單。
    """
    rows = [r for r in screen_text.splitlines() if r.strip()][-15:]
    options: list[tuple[str, str]] = []
    for row in rows:
        m = _MENU_OPTION_RE.match(row)
        if m and m.group(1) not in {k for k, _ in options}:
            options.append((m.group(1), m.group(2).strip()))
    return options


def dialog_signature(screen_text: str) -> str:
    """
    確認畫面的「指紋」：最下方選單的選項，加上選項上方幾行（通常是它要執行的指令或要改的檔案）。
    按下權限按鈕時比對指紋，確定還是「當初通知你的那一個」確認畫面才送鍵，
    避免舊按鈕回答到後來才跳出來的另一個確認（例如變成 rm 指令）。
    底部的狀態列、計時器不算在內，所以不會因為時間在跑就誤判成畫面變了。
    """
    rows = [r for r in screen_text.splitlines() if r.strip()][-25:]
    option_rows = [i for i, r in enumerate(rows) if _MENU_OPTION_RE.match(r)]
    if not option_rows:
        return ""
    first, last = option_rows[0], option_rows[-1]
    block = rows[max(0, first - 8):last + 1]
    return "\n".join(re.sub(r"[│|╭╮╰╯─\s]+", " ", r).strip() for r in block)


def is_shell(command: str) -> bool:
    """前景程式是 shell（zsh、bash…）：這是一般終端機，或 claude 已經結束，只剩下提示字元。"""
    return command.strip().lower() in SHELLS
