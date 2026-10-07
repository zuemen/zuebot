"""
setup_hooks.py —— 把 zuebot 的 hook 合併進 Claude Code 的設定檔（~/.claude/settings.json）

用法：
  python -m zuebot.setup_hooks            安裝（或更新）hook，安裝前會先備份設定檔
  python -m zuebot.setup_hooks --check    只檢查有沒有裝好，不修改任何東西
  python -m zuebot.setup_hooks --remove   移除 zuebot 的 hook

設計重點：
  - 不會動到你原本的其他設定或其他 hook，只增刪「指令裡含有 zuebot 的 hook.py」的項目。
  - 可以重複執行：已經裝過就先移除舊的再裝新的，不會越裝越多。
  - hook 用「目前這個 Python」的完整路徑執行（通常是專案的 .venv），
    這樣不受 PATH 影響，也不會用到太舊的系統 Python。
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_SCRIPT = Path(__file__).resolve().with_name("hook.py")
# 要掛 hook 的事件：Stop＝回覆完成、Notification＝需要你注意、PermissionRequest＝跳出權限確認
EVENTS = ("Stop", "Notification", "PermissionRequest")
HOOK_TIMEOUT = 10   # 秒；hook 本身通常 0.1 秒內就結束


def hook_command(python: str | None = None) -> str:
    """組出 hook 要執行的指令，例如：/Users/你/zuebot/.venv/bin/python /Users/你/zuebot/src/zuebot/hook.py"""
    return f"{shlex.quote(python or sys.executable)} {shlex.quote(str(HOOK_SCRIPT))}"


def is_zuebot_hook(hook: dict) -> bool:
    """判斷一個 hook 項目是不是 zuebot 裝的（指令裡有 zuebot 而且有 hook.py）。"""
    cmd = str(hook.get("command", ""))
    return "zuebot" in cmd and "hook.py" in cmd


def load_settings(path: Path = SETTINGS) -> dict:
    """讀設定檔；不存在就回傳空設定。格式壞掉就丟出 ValueError，避免覆蓋掉你的設定。"""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 的內容不是 JSON 物件")
    return data


def remove_ours(settings: dict) -> int:
    """從設定中移除所有 zuebot 的 hook，回傳移除了幾個。空掉的群組和事件也一併清掉。"""
    removed = 0
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                new_groups.append(group)
                continue
            kept = [h for h in group["hooks"] if not (isinstance(h, dict) and is_zuebot_hook(h))]
            removed += len(group["hooks"]) - len(kept)
            if kept:
                new_groups.append({**group, "hooks": kept})
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return removed


def add_ours(settings: dict, command: str) -> None:
    """在每個事件加上一個 zuebot 的 hook 群組（不設 matcher＝所有情況都觸發）。"""
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        hooks.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]}
        )


def check(settings: dict) -> dict[str, bool]:
    """回傳每個事件是否已裝好 zuebot 的 hook，例如 {"Stop": True, "Notification": False, ...}。"""
    result = {}
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    for event in EVENTS:
        result[event] = any(
            isinstance(h, dict) and is_zuebot_hook(h)
            for group in hooks.get(event, []) if isinstance(group, dict)
            for h in group.get("hooks", []) if isinstance(group.get("hooks"), list)
        )
    return result


def save_with_backup(settings: dict, path: Path = SETTINGS) -> Path | None:
    """先把舊設定檔複製成 settings.json.bak-時間，再寫入新設定。回傳備份檔路徑（原本沒有檔案就是 None）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_bytes(path.read_bytes())
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return backup


def main(argv: list[str] | None = None) -> int:
    """命令列入口：安裝、檢查或移除 hook。"""
    parser = argparse.ArgumentParser(prog="python -m zuebot.setup_hooks", description="安裝 zuebot 的 Claude Code hook")
    parser.add_argument("--check", action="store_true", help="只檢查，不修改")
    parser.add_argument("--remove", action="store_true", help="移除 zuebot 的 hook")
    parser.add_argument("--settings", default=str(SETTINGS), help="設定檔路徑（預設 ~/.claude/settings.json）")
    args = parser.parse_args(argv)
    path = Path(args.settings).expanduser()

    try:
        settings = load_settings(path)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"❌ {path} 格式有誤，為了不弄壞你的設定，沒有做任何修改：{e}")
        return 1

    if args.check:
        status = check(settings)
        for event, ok in status.items():
            print(f"{'✅' if ok else '❌'} {event} hook")
        return 0 if all(status.values()) else 1

    removed = remove_ours(settings)
    if not args.remove:
        add_ours(settings, hook_command())
    backup = save_with_backup(settings, path)
    if backup:
        print(f"📦 已備份原本的設定到 {backup}")
    if args.remove:
        print(f"🗑 已移除 {removed} 個 zuebot hook")
    else:
        print(f"✅ 已安裝 hook（{', '.join(EVENTS)}）到 {path}")
        print(f"   指令：{hook_command()}")
        print("   已經開著的 claude 要重開才會套用新的 hook。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
