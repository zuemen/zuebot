"""
state.py —— bot 的狀態，以及存檔／讀檔

存的東西：
  current        每個聊天室的「目前對象」（chat_id → session 名稱）
  watches        關注清單（session 名稱 → 要通知的 chat_id）
  events_offset  事件檔已經讀到第幾個位元組（重開 bot 不會重複通知、也不會漏掉）

跟 v0 的差別：
  - 存檔用「先寫暫存檔再改名」，寫到一半當機也不會留下壞掉的檔案。
  - 讀檔時如果檔案壞了，會把它改名成 .bad 保留下來，用空白狀態啟動，而不是整個 bot 起不來。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class State:
    """bot 的全部持久狀態。每次修改後呼叫 save() 存檔。"""

    def __init__(self, path: Path) -> None:
        """建立一個空白狀態，path 是存檔位置。"""
        self.path = path
        self.current: dict[str, str] = {}     # JSON 的鍵只能是字串，所以 chat_id 存成字串
        self.watches: dict[str, int] = {}
        self.events_offset: int | None = None  # None 代表「第一次啟動，從事件檔尾端開始」

    @classmethod
    def load(cls, path: Path) -> "State":
        """從檔案讀取狀態；檔案不存在就回傳空白狀態，檔案壞掉就備份後回傳空白狀態。"""
        state = cls(path)
        if not path.exists():
            return state
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            state.current = {str(k): str(v) for k, v in dict(data.get("current", {})).items()}
            state.watches = {str(k): int(v) for k, v in dict(data.get("watches", {})).items()}
            offset = data.get("events_offset", data.get("offset"))   # 相容 v0 的欄位名稱
            state.events_offset = int(offset) if offset is not None else None
        except (ValueError, TypeError, AttributeError, OSError) as e:
            backup = path.with_suffix(path.suffix + ".bad")
            log.warning("狀態檔損壞（%s），已備份到 %s，改用空白狀態啟動", e, backup)
            try:
                os.replace(path, backup)
            except OSError:
                pass
            return cls(path)
        return state

    def save(self) -> None:
        """原子地寫入狀態檔：先寫 .tmp，成功後才改名覆蓋正式檔案。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"current": self.current, "watches": self.watches, "events_offset": self.events_offset}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ── 目前對象 ──
    def get_current(self, chat_id: int) -> str | None:
        """取得某個聊天室的目前對象，沒設定就回傳 None。"""
        return self.current.get(str(chat_id))

    def set_current(self, chat_id: int, name: str) -> None:
        """設定某個聊天室的目前對象並存檔。"""
        self.current[str(chat_id)] = name
        self.save()

    # ── 關注清單 ──
    def watch(self, name: str, chat_id: int) -> None:
        """開始關注某個 CLI，完成或等待確認時通知 chat_id。"""
        self.watches[name] = int(chat_id)
        self.save()

    def unwatch(self, name: str) -> bool:
        """取消關注；原本就沒有關注時回傳 False。"""
        if self.watches.pop(name, None) is None:
            return False
        self.save()
        return True

    def watcher(self, name: str) -> int | None:
        """回傳正在關注這個 CLI 的 chat_id；沒人關注就回傳 None。"""
        return self.watches.get(name)

    def forget_session(self, name: str) -> None:
        """session 被關掉時呼叫：從關注清單和所有「目前對象」裡移除。"""
        self.watches.pop(name, None)
        self.current = {k: v for k, v in self.current.items() if v != name}
        self.save()
