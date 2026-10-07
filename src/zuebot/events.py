"""
events.py —— 讀取 hook 寫出的事件檔（events.jsonl，一行一個 JSON 事件）

運作方式：記住「上次讀到第幾個位元組」（offset），每次只讀新增的部分。

跟 v0 的差別：
  - 只處理到「最後一個換行」為止。hook 剛好寫到一半時，半行會留到下次再讀，
    不會因為 JSON 解析失敗而永遠漏掉那個事件。
  - 用二進位模式讀，offset 一定是位元組位置（文字模式的 tell() 對中文不可靠）。
  - 檔案超過 MAX_BYTES 且已全部讀完時，自動改名成 events.jsonl.1，避免無限長大。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024   # 事件檔超過 5MB 就輪替


class EventReader:
    """持續讀取事件檔新增內容的讀取器。"""

    def __init__(self, path: Path, offset: int | None = None) -> None:
        """
        path：事件檔位置。
        offset：上次讀到的位置；None 代表第一次啟動，從檔尾開始（舊事件不補發，免得一次跳出一堆通知）。
        """
        self.path = path
        self.offset = offset

    def _parse(self, chunk: bytes) -> list[dict]:
        """把一段完整的多行內容解析成事件清單，壞掉的行直接略過。"""
        events = []
        for raw in chunk.splitlines():
            if not raw.strip():
                continue
            try:
                ev = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                log.warning("略過無法解析的事件行：%r", raw[:200])
                continue
            if isinstance(ev, dict):
                events.append(ev)
        return events

    def _read_complete_lines(self) -> bytes:
        """從 offset 讀到最後一個換行，並把 offset 往前移；不完整的最後半行留著下次讀。"""
        with self.path.open("rb") as f:
            f.seek(self.offset or 0)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return b""
        self.offset = (self.offset or 0) + end + 1
        return data[:end + 1]

    def read_new(self) -> list[dict]:
        """讀取上次之後新增的所有完整事件。事件檔不存在時回傳空清單。"""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            if self.offset is None:
                self.offset = 0      # 檔案還沒出現：之後 hook 建立的新檔要從頭讀
            return []

        if self.offset is None:
            self.offset = size       # 第一次啟動：從尾端開始
        elif self.offset > size:
            log.info("事件檔變小了（被清空或換新），從頭開始讀")
            self.offset = 0

        events = self._parse(self._read_complete_lines())
        self._maybe_rotate(events)
        return events

    def _maybe_rotate(self, events: list[dict]) -> None:
        """
        檔案太大而且已經讀完時，改名成 .1（覆蓋舊的 .1），新事件會寫進 hook 自動建立的新檔。
        改名後再讀一次舊檔，撿回「最後一次讀取」和「改名」之間剛好寫進去的事件。
        """
        try:
            if self.offset is None or self.offset < MAX_BYTES or self.offset != self.path.stat().st_size:
                return
            old = self.path.with_name(self.path.name + ".1")
            os.replace(self.path, old)
            with old.open("rb") as f:
                f.seek(self.offset)
                leftover = f.read()
            events.extend(self._parse(leftover))
            self.offset = 0
            log.info("事件檔已輪替到 %s", old)
        except OSError as e:
            log.warning("事件檔輪替失敗：%s", e)
