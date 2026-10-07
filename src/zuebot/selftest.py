"""
selftest.py —— 在你的 Mac 上用「真的 claude」做 Phase 1 驗證

用法：
  python -m zuebot.selftest              互動式：遇到「信任資料夾」提示會問你要不要信任
  python -m zuebot.selftest --yes        全自動：信任提示直接同意（只會信任測試專用的暫存資料夾）
  python -m zuebot.selftest --skip-permission   不測權限確認畫面
  python -m zuebot.selftest --skip-brain        不測大腦（claude -p）
  python -m zuebot.selftest --keep              測完不關掉測試用的 CLI，方便你 attach 進去看

會做的事：
  1. 在 ~/.zuebot/selftest/ 底下開一個新資料夾，用 tmux 開一個 claude（session 名稱 zuebot-selftest）
  2. 偵測啟動畫面、信任資料夾提示
  3. 依序送出：單行、多行、中文與 emoji、很長的文字，確認每一則都「完整送出而且只送一次」，
     並確認 Stop hook 有觸發、拿得到最後回覆、記錄 hook 實際提供了哪些欄位
  4. 請 claude 執行一個無害的指令（echo），確認看得到權限確認畫面與對應的 hook 事件，然後按 Esc 拒絕
  5. 測試大腦（claude -p）能不能用你的訂閱回應
  6. 把結果寫到 ~/.zuebot/selftest-report.md ——有問題時把這個檔案內容貼給 Claude 就能據此修正

注意：會用掉幾則你的 Claude 訂閱額度（每則都只要求回覆一個短代碼）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from . import cli, config, screen, setup_hooks, tmux_ops
from .events import EventReader

SESSION = "zuebot-selftest"
REPLY_TIMEOUT = 180       # 每則測試最多等幾秒讓 claude 回覆
DUPLICATE_WAIT = 6        # 收到回覆後再多等幾秒，確認沒有被送出第二次


class Report:
    """收集測試結果：一邊印在終端機，一邊存起來最後寫成 markdown 檔。"""

    def __init__(self) -> None:
        """建立空的報告。"""
        self.lines: list[str] = []
        self.failures = 0
        self.warnings = 0

    def add(self, mark: str, text: str) -> None:
        """加一行結果，mark 是 ✅ ❌ ⚠️ ℹ️ 其中之一。"""
        if mark == "❌":
            self.failures += 1
        elif mark == "⚠️":
            self.warnings += 1
        line = f"{mark} {text}"
        print(line, flush=True)
        self.lines.append(f"- {line}")

    def section(self, title: str) -> None:
        """開始新的一節。"""
        print(f"\n=== {title} ===", flush=True)
        self.lines.append(f"\n## {title}\n")

    def screen(self, title: str, text: str) -> None:
        """把畫面原文存進報告（只存檔，不印在終端機，避免洗版）。"""
        self.lines.append(f"\n<details><summary>{title}</summary>\n\n```\n{screen.recent(text, 40)}\n```\n</details>\n")

    def save(self, path: Path) -> None:
        """寫成 markdown 檔。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        head = (f"# zuebot 自我檢查報告\n\n時間：{time.strftime('%Y-%m-%d %H:%M:%S')}　"
                f"失敗 {self.failures} 項、警告 {self.warnings} 項\n")
        path.write_text(head + "\n".join(self.lines) + "\n", encoding="utf-8")


def run_quiet(cmd: list[str]) -> str:
    """執行一個簡單指令並回傳輸出（找不到指令就回傳空字串）。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class PaneEvents:
    """
    只收集某一個 pane 的 hook 事件，並且保留「還沒處理」的事件。
    （一次讀檔可能同時讀到好幾個事件，不能讀到第一個 Stop 就把後面的丟掉，否則抓不到重複送出）
    """

    def __init__(self, reader: EventReader) -> None:
        """reader 是已經定位到檔尾的事件讀取器。"""
        self.reader = reader
        self.pane = ""
        self.pending: list[dict] = []

    def poll(self) -> None:
        """把事件檔新增的、屬於這個 pane 的事件加進待處理清單。"""
        self.pending += [ev for ev in self.reader.read_new() if ev.get("pane") == self.pane]

    def clear(self) -> None:
        """丟掉目前所有待處理事件（開始下一項測試前呼叫）。"""
        self.poll()
        self.pending.clear()

    def take_all(self) -> list[dict]:
        """取出所有待處理事件。"""
        self.poll()
        events, self.pending = self.pending, []
        return events

    async def wait_for(self, event: str, timeout: float) -> dict | None:
        """等待指定種類的事件（例如 Stop），取出並回傳它；逾時回傳 None。排在它後面的事件會保留。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            self.poll()
            for i, ev in enumerate(self.pending):
                if ev.get("event") == event:
                    del self.pending[i]
                    return ev
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.5)


async def check_environment(cfg: config.Config, rep: Report) -> None:
    """第 1 步：檢查版本與 hook 設定。"""
    rep.section("1. 環境")
    rep.add("ℹ️", f"tmux：{await tmux_ops.version()}")
    rep.add("ℹ️", f"claude：{run_quiet([cfg.claude_cmd, '--version']) or '（讀不到版本）'}")
    rep.add("ℹ️", f"Python：{sys.version.split()[0]}（{sys.executable}）")
    auth = run_quiet([cfg.claude_cmd, "auth", "status", "--text"])
    rep.add("ℹ️", f"claude 登入狀態：{auth.splitlines()[0] if auth else '（讀不到）'}")
    try:
        status = setup_hooks.check(setup_hooks.load_settings())
    except (ValueError, json.JSONDecodeError) as e:
        rep.add("❌", f"~/.claude/settings.json 格式有誤：{e}")
        return
    for event, ok in status.items():
        rep.add("✅" if ok else "❌", f"{event} hook {'已設定' if ok else '沒有設定（請執行 python -m zuebot.setup_hooks）'}")


async def start_cli(cfg: config.Config, rep: Report, auto_yes: bool) -> str | None:
    """第 2 步：開測試用的 CLI，處理信任提示，回傳 pane id（失敗回傳 None）。"""
    rep.section("2. 開啟 CLI 與信任資料夾提示")
    workdir = cfg.data_dir / "selftest" / time.strftime("%Y%m%d-%H%M%S")
    workdir.mkdir(parents=True, exist_ok=True)
    if await tmux_ops.session_exists(SESSION):
        await tmux_ops.kill_session(SESSION)
    await tmux_ops.new_session(SESSION, str(workdir), cfg.claude_cmd)
    pane = next((s.pane_id for s in await tmux_ops.list_sessions() if s.name == SESSION), "")
    rep.add("✅", f"已開 tmux session「{SESSION}」（pane {pane}），資料夾 {workdir}")

    state, text = await cli.wait_for_startup(SESSION, 60)
    rep.screen("啟動後的畫面", text)
    if state == screen.TRUST:
        rep.add("✅", "偵測到「信任資料夾」提示（bot 開新 CLI 時會用按鈕問你）")
        answer = "y" if auto_yes else input("要信任這個測試資料夾並繼續嗎？[Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            rep.add("⚠️", "你選擇不信任，後面的測試略過")
            return None
        await cli.accept_trust(SESSION)
        state, text = await cli.wait_for_startup(SESSION, 60)
        rep.screen("信任之後的畫面", text)
    else:
        rep.add("ℹ️", f"沒有出現信任提示（狀態：{screen.STATE_LABELS[state]}）。若這資料夾是新的，代表偵測規則可能需要調整，請看報告裡的畫面")
    if state == screen.LOGIN:
        rep.add("❌", "claude 還沒登入：請在終端機執行 claude，用 /login 登入你的訂閱帳號")
        return None
    if state != screen.IDLE:
        rep.add("❌", f"等不到可輸入的畫面（最後狀態：{screen.STATE_LABELS[state]}），請看報告裡的畫面")
        return None
    rep.add("✅", "claude 已啟動，輸入框可以使用")
    return pane


async def test_paste(rep: Report, events: PaneEvents) -> None:
    """第 3 步：各種文字貼上測試 + Stop hook 欄位驗證。"""
    rep.section("3. 送字與 Stop hook")
    filler = "這是填充用的長文字，不需要理會內容。" * 120
    cases = [
        ("單行", "zuebot 自我測試：請只回覆 ZB1OK 這幾個字，不要做任何其他事。", "ZB1OK"),
        ("多行", "zuebot 自我測試（多行）。\n第二行。\n第三行：請只回覆 ZB2OK，不要做任何其他事。", "ZB2OK"),
        ("中文、全形符號與 emoji", "測試「全形」符號！？、還有 emoji 😀🚀：請只回覆 ZB3OK，不要做其他事。", "ZB3OK"),
        (f"很長的文字（約 {len(filler)} 字）", f"以下是長文字測試：\n{filler}\n請只回覆 ZB4OK，不要做其他事。", "ZB4OK"),
    ]
    keys_seen: set[str] = set()
    for label, text, token in cases:
        events.clear()   # 清掉之前的事件
        note = await cli.deliver(SESSION, text)
        if note:
            rep.add("⚠️", f"{label}：{note}")
        stop = await events.wait_for("Stop", REPLY_TIMEOUT)
        if stop is None:
            rep.add("❌", f"{label}：{REPLY_TIMEOUT} 秒內沒有收到 Stop hook 事件")
            rep.screen(f"{label}：逾時時的畫面", await tmux_ops.capture(SESSION, 60))
            continue
        keys_seen.update(stop.get("keys", []))
        reply = str(stop.get("reply", ""))
        if token in reply:
            rep.add("✅", f"{label}：送出成功，Stop hook 拿到回覆「{reply.strip()[:40]}」")
        else:
            rep.add("❌", f"{label}：回覆裡沒有 {token}，回覆是「{reply.strip()[:80]}」")
            rep.screen(f"{label}：畫面", await tmux_ops.capture(SESSION, 60))
        dup = await events.wait_for("Stop", DUPLICATE_WAIT)
        if dup is not None:
            rep.add("❌", f"{label}：又收到第二次 Stop，訊息可能被拆成好幾次送出")
    if keys_seen:
        rep.add("ℹ️", f"Stop hook 實際提供的欄位：{', '.join(sorted(keys_seen))}")
        mark = "✅" if "last_assistant_message" in keys_seen else "⚠️"
        rep.add(mark, "有 last_assistant_message 欄位" if mark == "✅" else "沒有 last_assistant_message，改用讀 transcript 的備援")


async def test_permission(rep: Report, events: PaneEvents) -> None:
    """第 4 步：權限確認畫面偵測 + Notification / PermissionRequest hook。"""
    rep.section("4. 權限確認畫面")
    events.clear()
    await cli.deliver(SESSION, "zuebot 自我測試：請用 Bash 工具執行 `echo zuebot-permission-test`，這是測試，直接執行即可。")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 120
    state, text, seen = screen.UNKNOWN, "", []
    while loop.time() < deadline:
        seen += events.take_all()
        state, text = await cli.get_state(SESSION, 60)
        if state == screen.PERMISSION or any(ev.get("event") == "Stop" for ev in seen):
            break
        await asyncio.sleep(1)
    rep.screen("權限確認時的畫面", text)
    if state != screen.PERMISSION:
        if any(ev.get("event") == "Stop" for ev in seen):
            rep.add("⚠️", "claude 沒有問權限就直接執行了（你的設定可能已允許 Bash），這項無法測試")
        else:
            rep.add("❌", "沒有偵測到權限確認畫面，請看報告裡的畫面調整 screen.py 的規則")
        return
    rep.add("✅", "偵測到權限確認畫面")
    await asyncio.sleep(10)   # Notification 的 permission_prompt 大約 6 秒後才會發出
    seen += events.take_all()
    kinds = {f"{ev.get('event')}:{ev.get('notification_type', '')}".rstrip(":") for ev in seen}
    rep.add("ℹ️", f"這段期間收到的 hook 事件：{', '.join(sorted(kinds)) or '（沒有）'}")
    perm = next((ev for ev in seen if ev.get("event") == "PermissionRequest"), None)
    rep.add("✅" if perm else "⚠️", f"PermissionRequest hook：{'有收到，工具 ' + str(perm.get('tool_name')) if perm else '沒有收到（會改用 Notification）'}")
    note = any(ev.get("notification_type") == "permission_prompt" for ev in seen)
    rep.add("✅" if note else "⚠️", f"Notification（permission_prompt）：{'有收到' if note else '沒有收到'}")
    if not perm and not note:
        rep.add("❌", "兩種權限事件都沒收到，bot 會靠備援的畫面偵測，但通知會比較慢")
    await tmux_ops.send_key(SESSION, "esc")
    await asyncio.sleep(2)
    rep.add("✅" if (await cli.get_state(SESSION))[0] != screen.PERMISSION else "❌", "按 Esc 拒絕後離開了確認畫面")


async def test_brain(cfg: config.Config, rep: Report) -> None:
    """第 5 步：大腦（claude -p）能不能用。Phase 2 之後才有 brain 模組。"""
    rep.section("5. 大腦（claude -p）")
    try:
        from . import brain
    except ImportError:
        rep.add("ℹ️", "還沒有大腦模組，略過")
        return
    b = brain.Brain(cfg)
    start = time.time()
    try:
        text = await b.ping()
        rep.add("✅", f"摘要模型（{cfg.summary_model}）有回應（{time.time() - start:.1f} 秒）：{text[:60]}")
    except brain.BrainError as e:
        rep.add("❌", f"大腦呼叫失敗：{e}")
        return
    start = time.time()
    context = {"clis": [{"name": "競賽", "path": "/Users/me/projects/contest", "state": "執行中", "watched": False},
                        {"name": "medssi", "path": "/Users/me/projects/medssi", "state": "閒置（等你輸入）", "watched": True}],
               "current": None}
    try:
        plan = await b.plan("跟競賽那個說改用 v2 資料集，結束跟我說", context, [], [])
    except brain.BrainError as e:
        rep.add("❌", f"大腦（{cfg.brain_model}）產生計畫失敗：{e}")
        return
    rep.add("ℹ️", f"大腦（{cfg.brain_model}）花了 {time.time() - start:.1f} 秒，格式來源：{b.last_mode}")
    sends = [a for a in plan.actions if a.get("tool") == "send_text"]
    if sends and sends[0].get("name") == "競賽" and sends[0].get("text", "").strip() == "改用 v2 資料集":
        rep.add("✅", "大腦正確理解「轉貼原話」：send_text 競賽「改用 v2 資料集」")
    else:
        rep.add("⚠️", f"大腦的計畫跟預期不同（不影響安全，bot 仍會檢查原話）：{plan.actions}")


async def main_async(args: argparse.Namespace) -> int:
    """依序執行所有測試，最後寫報告。"""
    config.load_dotenv(args.env_file)
    cfg = config.load_config(require_token=False)
    tmux_ops.configure(cfg.tmux_bin)
    rep = Report()
    report_path = cfg.data_dir / "selftest-report.md"
    reader = EventReader(cfg.events_file, None)
    reader.read_new()   # 從事件檔尾端開始，只看這次測試產生的事件
    events = PaneEvents(reader)
    try:
        await check_environment(cfg, rep)
        pane = await start_cli(cfg, rep, args.yes)
        if pane:
            events.pane = pane
            await test_paste(rep, events)
            if not args.skip_permission:
                await test_permission(rep, events)
        if not args.skip_brain:
            await test_brain(cfg, rep)
    except tmux_ops.TmuxError as e:
        rep.add("❌", f"tmux 操作失敗：{e}")
    except KeyboardInterrupt:
        rep.add("⚠️", "你中斷了測試")
    finally:
        if not args.keep and await tmux_ops.session_exists(SESSION):
            await tmux_ops.kill_session(SESSION)
        rep.save(report_path)
    print(f"\n結果：失敗 {rep.failures} 項、警告 {rep.warnings} 項。完整報告：{report_path}")
    if rep.failures:
        print("有失敗項目時，把報告內容貼給 Claude，請它依報告修正。")
    return 1 if rep.failures else 0


def main(argv: list[str] | None = None) -> int:
    """命令列入口。"""
    parser = argparse.ArgumentParser(prog="python -m zuebot.selftest", description="用真的 claude 驗證 zuebot")
    parser.add_argument("--yes", action="store_true", help="信任提示自動同意（只限測試資料夾）")
    parser.add_argument("--skip-permission", action="store_true", help="不測權限確認畫面")
    parser.add_argument("--skip-brain", action="store_true", help="不測大腦")
    parser.add_argument("--keep", action="store_true", help="測完不關掉測試用的 CLI")
    parser.add_argument("--env-file", help="指定 .env 檔")
    parser.add_argument("--debug", action="store_true", help="印出每個 tmux 指令")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return asyncio.run(main_async(args))
    except config.ConfigError as e:
        print(f"❌ 設定錯誤：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
