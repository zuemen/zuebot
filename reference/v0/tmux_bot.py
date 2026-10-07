"""
tmux_bot.py —— 用 Telegram 控制這台電腦上「跑在 tmux 裡」的 Claude Code CLI

能做的事：
  /list                     列出所有 tmux session（你的每個 CLI）
  /use 競賽                 設定「目前對象」，之後直接打字就會送進去
  （直接打字）               送進目前對象的 CLI，並自動關注，完成時回報
  /look 競賽 [行數]          看那個 CLI 目前的畫面
  /send 競賽 訊息            不切換對象，直接送一段訊息
  /paste 競賽                下一則訊息（可多行）原封不動貼進去
  /watch 競賽 /unwatch 競賽  關注／取消關注（完成或等你確認時通知）
  /key 競賽 1                送按鍵：1 2 3 enter esc up down tab ctrl-c（用於權限確認選單）
  /new 名稱 資料夾 [第一個任務]  開新的 tmux + claude
  /kill 名稱 yes            關閉某個 session（要加 yes 才會執行）

原理：
  讀畫面 = tmux capture-pane；打字 = tmux paste-buffer + Enter；
  「完成了沒」= Claude Code 的 Stop hook（hook.py）寫事件到 events.jsonl，bot 每 2 秒讀一次。
"""

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ───────────── 設定（從環境變數讀） ─────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
ALLOWED_ROOT = Path(os.environ.get("ALLOWED_ROOT", str(Path.home()))).expanduser().resolve()
CLAUDE_CMD = os.environ.get("CLAUDE_CMD", "claude")      # 開新 CLI 時執行的指令
TMUX = os.environ.get("TMUX_BIN", "tmux")
DATA_DIR = Path.home() / ".claude-tg"
EVENTS = Path(os.environ.get("CLAUDE_TG_EVENTS", str(DATA_DIR / "events.jsonl")))
STATE_FILE = DATA_DIR / "bot_state.json"
MAX_TG = 3900                 # Telegram 單則上限 4096，留餘裕
PASTE_TIMEOUT = 300           # /paste 等待下一則訊息的秒數
NAME_RE = re.compile(r"^[\w\-一-鿿]{1,30}$")   # session 名稱只允許中英數、底線、連字號
KEYS = {"enter": "Enter", "esc": "Escape", "up": "Up", "down": "Down", "tab": "Tab",
        "ctrl-c": "C-c", "1": "1", "2": "2", "3": "3", "y": "y", "n": "n"}

# ───────────── 狀態（會存檔，重開 bot 不會遺失） ─────────────
# current：chat_id -> 目前對象；watches：session -> chat_id；offset：events 檔讀到哪
state = {"current": {}, "watches": {}, "offset": None}
paste_armed: dict[int, tuple[str, float]] = {}   # chat_id -> (session, 到期時間)，不存檔


def load_state() -> None:
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))


def save_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ───────────── tmux 操作 ─────────────
def tmux(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    """執行一個 tmux 指令並回傳結果（不丟例外，由呼叫端看 returncode）"""
    return subprocess.run([TMUX, *args], input=input_text, capture_output=True, text=True, timeout=10)


def list_sessions() -> list[dict]:
    """列出所有 tmux session：名稱、工作目錄、前景程式"""
    r = tmux("list-sessions", "-F", "#{session_name}\t#{pane_current_path}\t#{pane_current_command}")
    if r.returncode != 0:
        return []   # 沒有任何 session 時 tmux 會回錯誤
    out = []
    for line in r.stdout.strip().splitlines():
        name, path, cmd = (line.split("\t") + ["", "", ""])[:3]
        out.append({"name": name, "path": path, "cmd": cmd})
    return out


def session_exists(name: str) -> bool:
    return any(s["name"] == name for s in list_sessions())


def capture(name: str, lines: int = 40) -> str:
    """讀取畫面最後 N 行（-J 會把被自動換行的長行接回來）"""
    r = tmux("capture-pane", "-p", "-J", "-t", name, "-S", f"-{lines}")
    return r.stdout.rstrip() if r.returncode == 0 else f"（讀取失敗：{r.stderr.strip()}）"


def paste_text(name: str, text: str) -> None:
    """
    把文字貼進 CLI 再按 Enter。
    用 paste-buffer -p（bracketed paste）而不是 send-keys，
    這樣多行文字不會在中間的換行就被送出。
    """
    tmux("load-buffer", "-b", "tgbot", "-", input_text=text)
    tmux("paste-buffer", "-p", "-d", "-b", "tgbot", "-t", name)
    time.sleep(0.5)                       # 給 Claude 的輸入框一點時間吃下貼上的內容
    tmux("send-keys", "-t", name, "Enter")


# ───────────── 小工具 ─────────────
async def send(app: Application, chat_id: int, text: str) -> None:
    """傳到 Telegram，太長就自動切段"""
    text = text or "（空白）"
    for i in range(0, len(text), MAX_TG):
        await app.bot.send_message(chat_id=chat_id, text=text[i:i + MAX_TG])


def authorized(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ALLOWED_USERS)


async def need_session(update: Update, name: str | None) -> str | None:
    """確認 session 存在，不存在就回覆錯誤並列出現有的"""
    if name and session_exists(name):
        return name
    names = "、".join(s["name"] for s in list_sessions()) or "（目前沒有）"
    await update.message.reply_text(f"找不到 session「{name}」。現有：{names}")
    return None


def watch(name: str, chat_id: int) -> None:
    state["watches"][name] = chat_id
    save_state()


# ───────────── Telegram 指令 ─────────────
HELP = __doc__.split("原理")[0].strip()


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text(f"未授權。你的 user id 是 {update.effective_user.id}")
        return
    await update.message.reply_text(HELP)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    sessions = list_sessions()
    if not sessions:
        await update.message.reply_text("目前沒有 tmux session。用 /new 開一個，或在電腦上用 cc 腳本開。")
        return
    cur = state["current"].get(str(update.effective_chat.id))
    lines = []
    for s in sessions:
        mark = "👉" if s["name"] == cur else "  "
        eye = " 👀" if s["name"] in state["watches"] else ""
        lines.append(f"{mark} {s['name']}{eye}  [{s['cmd']}]\n    {s['path']}")
    await update.message.reply_text("\n".join(lines) + "\n\n👉 目前對象　👀 關注中")


async def cmd_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    name = await need_session(update, ctx.args[0] if ctx.args else None)
    if not name:
        return
    state["current"][str(update.effective_chat.id)] = name
    save_state()
    await update.message.reply_text(f"👉 目前對象：{name}。之後直接打字就會送進去。")


async def cmd_look(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    name = ctx.args[0] if ctx.args else state["current"].get(str(update.effective_chat.id))
    name = await need_session(update, name)
    if not name:
        return
    lines = int(ctx.args[1]) if len(ctx.args) > 1 and ctx.args[1].isdigit() else 40
    await send(ctx.application, update.effective_chat.id, f"🖥 [{name}] 目前畫面：\n\n{capture(name, lines)}")


async def deliver(update: Update, name: str, text: str) -> None:
    """送訊息進 CLI，並自動關注，完成時會回報"""
    paste_text(name, text)
    watch(name, update.effective_chat.id)
    await update.message.reply_text(f"📨 已送到 [{name}]，完成時會通知你。")


async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    parts = (update.message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("用法：/send <名稱> <訊息>")
        return
    name = await need_session(update, parts[1])
    if name:
        await deliver(update, name, parts[2])


async def cmd_paste(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    name = ctx.args[0] if ctx.args else state["current"].get(str(update.effective_chat.id))
    name = await need_session(update, name)
    if not name:
        return
    paste_armed[update.effective_chat.id] = (name, time.time() + PASTE_TIMEOUT)
    await update.message.reply_text(f"📋 好，下一則訊息會原封不動貼進 [{name}]（5 分鐘內有效）。")


async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    name = await need_session(update, ctx.args[0] if ctx.args else None)
    if not name:
        return
    watch(name, update.effective_chat.id)
    tail = "\n".join(capture(name, 15).splitlines()[-15:])
    await send(ctx.application, update.effective_chat.id,
               f"👀 開始關注 [{name}]，完成或等你確認時會通知。\n目前畫面最後幾行：\n\n{tail}")


async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    name = ctx.args[0] if ctx.args else ""
    if state["watches"].pop(name, None) is None:
        await update.message.reply_text(f"[{name}] 本來就沒有在關注")
        return
    save_state()
    await update.message.reply_text(f"🙈 已停止關注 [{name}]")


async def cmd_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if len(ctx.args) != 2 or ctx.args[1].lower() not in KEYS:
        await update.message.reply_text(f"用法：/key <名稱> <{' / '.join(KEYS)}>")
        return
    name = await need_session(update, ctx.args[0])
    if not name:
        return
    tmux("send-keys", "-t", name, KEYS[ctx.args[1].lower()])
    watch(name, update.effective_chat.id)
    await asyncio.sleep(1.5)                       # 等畫面更新後回傳，讓你確認按對了
    tail = "\n".join(capture(name, 12).splitlines()[-12:])
    await send(ctx.application, update.effective_chat.id, f"⌨️ 已對 [{name}] 按下 {ctx.args[1]}\n\n{tail}")


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    parts = (update.message.text or "").split(maxsplit=3)
    if len(parts) < 3:
        await update.message.reply_text("用法：/new <名稱> <資料夾> [第一個任務]")
        return
    name, folder = parts[1], parts[2]
    first = parts[3] if len(parts) > 3 else ""
    if not NAME_RE.match(name):
        await update.message.reply_text("名稱只能用中英文、數字、底線、連字號，30 字以內")
        return
    if session_exists(name):
        await update.message.reply_text(f"已經有 [{name}] 了")
        return
    cwd = Path(folder).expanduser().resolve()
    if not cwd.is_dir() or (cwd != ALLOWED_ROOT and ALLOWED_ROOT not in cwd.parents):
        await update.message.reply_text(f"資料夾不存在，或不在允許範圍 {ALLOWED_ROOT} 底下")
        return
    r = tmux("new-session", "-d", "-s", name, "-c", str(cwd), CLAUDE_CMD)
    if r.returncode != 0:
        await update.message.reply_text(f"開啟失敗：{r.stderr.strip()}")
        return
    state["current"][str(update.effective_chat.id)] = name
    watch(name, update.effective_chat.id)
    await update.message.reply_text(f"🆕 已開 [{name}]（{cwd}），設為目前對象。")
    if first:
        await asyncio.sleep(6)                     # 等 claude 啟動完成再送任務
        await deliver(update, name, first)
    else:
        await asyncio.sleep(4)
        tail = "\n".join(capture(name, 15).splitlines()[-15:])
        await send(ctx.application, update.effective_chat.id,
                   f"畫面（第一次在某資料夾開 claude 可能會問是否信任資料夾，可用 /key {name} enter 確認）：\n\n{tail}")


async def cmd_kill(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if len(ctx.args) != 2 or ctx.args[1] != "yes":
        await update.message.reply_text("為了避免誤關，請輸入：/kill <名稱> yes")
        return
    name = await need_session(update, ctx.args[0])
    if not name:
        return
    tmux("kill-session", "-t", name)
    state["watches"].pop(name, None)
    save_state()
    await update.message.reply_text(f"🗑 已關閉 [{name}]")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """一般文字：先看是不是 /paste 等待中的內容，否則送進目前對象"""
    if not authorized(update):
        return
    chat_id = update.effective_chat.id
    armed = paste_armed.pop(chat_id, None)
    if armed and time.time() < armed[1]:
        name = await need_session(update, armed[0])
    else:
        if armed:
            await update.message.reply_text("（/paste 已逾時，改送到目前對象）")
        name = state["current"].get(str(chat_id))
        if not name:
            await update.message.reply_text("還沒設定目前對象，先 /use <名稱> 或 /list 看看")
            return
        name = await need_session(update, name)
    if name:
        await deliver(update, name, update.message.text)


# ───────────── 背景：讀 hook 事件並通知 ─────────────
async def event_loop(app: Application) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS.touch(exist_ok=True)
    if state["offset"] is None or state["offset"] > EVENTS.stat().st_size:
        state["offset"] = EVENTS.stat().st_size   # 第一次啟動：從檔尾開始，舊事件不補發
    while True:
        try:
            with EVENTS.open("r", encoding="utf-8") as f:
                f.seek(state["offset"])
                new_lines = f.readlines()
                state["offset"] = f.tell()
            if new_lines:
                save_state()
            for line in new_lines:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = ev.get("session", "")
                chat_id = state["watches"].get(name)
                if not chat_id:
                    continue                          # 沒在關注的 CLI 不打擾你
                if ev.get("event") == "Stop":
                    reply = (ev.get("reply") or "").strip()
                    if not reply:                     # 抓不到回覆文字時，改附畫面
                        reply = "（抓不到回覆文字，附上畫面）\n\n" + capture(name, 30)
                    await send(app, chat_id, f"✅ [{name}] 這輪完成：\n\n{reply}")
                elif ev.get("event") == "Notification":
                    tail = "\n".join(capture(name, 15).splitlines()[-15:])
                    await send(app, chat_id,
                               f"🔔 [{name}] 在等你：{ev.get('message', '')}\n\n{tail}\n\n"
                               f"可用 /key {name} 1（或 enter / esc）回應")
        except Exception as e:  # noqa: BLE001
            print("event_loop 錯誤：", repr(e))
        await asyncio.sleep(2)


async def post_init(app: Application) -> None:
    app.create_task(event_loop(app))


def main() -> None:
    if not ALLOWED_USERS:
        print("⚠️ 還沒設定 ALLOWED_USER_IDS：先傳 /start 給 bot 取得你的 user id，再設定後重開。")
    load_state()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    for cmd, fn in [("start", cmd_start), ("help", cmd_start), ("list", cmd_list), ("use", cmd_use),
                    ("look", cmd_look), ("send", cmd_send), ("paste", cmd_paste), ("watch", cmd_watch),
                    ("unwatch", cmd_unwatch), ("key", cmd_key), ("new", cmd_new), ("kill", cmd_kill)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print(f"Bot 啟動。允許的資料夾根目錄：{ALLOWED_ROOT}")
    app.run_polling()


if __name__ == "__main__":
    main()
