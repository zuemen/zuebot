"""
brain.py —— 大腦：把你的口語轉成工具呼叫，並把畫面／回覆整理成像助理一樣的報告

實作方式（已定案，見 docs/research-notes.md）：
  呼叫 `claude -p`，用你的 Claude 訂閱登入，不需要 API key。
  安全限制（大腦拿不到任何可以直接操作電腦的能力）：
    --tools ""              關掉所有內建工具（沒有 Bash、Read、Edit…）
    --strict-mcp-config     不載入任何 MCP 伺服器
    --disable-slash-commands  不載入 skills
    --no-session-persistence  不留下對話紀錄
    --disallowedTools mcp__*  明確禁止所有 MCP 工具（--strict-mcp-config 擋不住「外掛」提供的 MCP 伺服器）
    --safe-mode             關掉外掛、MCP、hook、CLAUDE.md（較新的 claude 才有，會先檢查 --help 再決定要不要加）
  大腦只能回傳 JSON 說「想做什麼」，由 bot 交給 tools.py 驗證後執行。

  另外：
    - 環境變數 ZUEBOT_BRAIN=1，讓我們自己的 hook 略過，不會產生假的「完成」事件
    - 拿掉 TMUX / TMUX_PANE，避免被當成某個 tmux 裡的 CLI
    - 在空的工作資料夾執行，不會讀到任何專案的 CLAUDE.md
    - 不用 --bare：它會跳過讀取 macOS 鑰匙圈，訂閱登入就不能用了
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

TOOLS = ["list_clis", "read_screen", "show_raw", "send_text", "send_key", "watch", "unwatch",
         "set_current", "new_cli", "arm_paste", "kill_cli", "ask_choice"]

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "給使用者看的回覆（繁體中文）"},
        "done": {"type": "boolean", "description": "false＝要先看 read_screen/list_clis 的結果再回答"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": TOOLS},
                    "name": {"type": "string"},
                    "text": {"type": "string"},
                    "key": {"type": "string"},
                    "cwd": {"type": "string"},
                    "first_prompt": {"type": "string"},
                    "lines": {"type": "integer"},
                    "candidates": {"type": "array", "items": {"type": "string"}},
                    "then_tool": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["tool"],
            },
        },
    },
    "required": ["reply", "done", "actions"],
}

PLAN_SYSTEM = """你是 zuebot，使用者的「CLI 管家」。使用者在手機 Telegram 上用口語指揮他電腦上好幾個跑在 tmux 裡的 Claude Code CLI。
你不能直接操作電腦，只能輸出一個 JSON 物件，列出要 bot 執行的工具動作。bot 會驗證並執行，危險動作 bot 會再向使用者確認。

輸出格式（只輸出這個 JSON，不要任何其他文字）：
{"reply": "給使用者的話", "done": true 或 false, "actions": [ {"tool": "...", ...}, ... ]}

可用工具（name 一律用「目前的 CLI」清單裡的確切名稱）：
- list_clis {}：列出所有 CLI 和狀態
- read_screen {name, lines?}：讀某個 CLI 的畫面給你看（結果會在下一輪的「觀察結果」給你）
- show_raw {name, lines?}：把畫面原文直接給使用者（只有使用者說「給我原文」「原始畫面」時才用）
- send_text {name, text}：把文字送進 CLI（會自動關注、完成時回報）
- send_key {name, key}：送按鍵，key 只能是 1 2 3 y n enter esc up down tab ctrl-c
- watch {name}／unwatch {name}：關注／取消關注（關注的 CLI 完成或需要確認時會通知使用者）
- set_current {name}：設為目前對象
- new_cli {name, cwd, first_prompt?}：開新的 CLI；name 用簡短的中英文或數字（不能有空白、冒號、句點）
- arm_paste {name}：使用者的「下一則訊息」會原封不動貼進這個 CLI
- kill_cli {name}：關閉 CLI
- ask_choice {candidates, then_tool, question, 以及 then_tool 需要的其他欄位}：指稱有歧義時讓使用者從候選中選

規則：
1. 【原話轉貼】send_text 的 text、new_cli 的 first_prompt，必須從使用者訊息中「逐字複製」一段連續文字，不可改寫、翻譯、增刪任何字、不可加引號。
   例：「跟競賽那個說改用 v2 資料集」→ text 是「改用 v2 資料集」。
   例：「開一個新的 CLI 在 ~/projects/thesis，幫我整理 related work」→ first_prompt 是「幫我整理 related work」。
2. 【不可猜】使用者的指稱如果同時符合好幾個 CLI，用 ask_choice 列出候選，不要自己挑一個。完全找不到就在 reply 說明並列出現有的。
3. 【先看再答】要回答「在幹嘛、跑到哪、有沒有在問權限」之類的問題，先輸出 read_screen 並設 done=false，reply 留空字串；
   拿到觀察結果後再輸出最後的答案（done=true）。已經有觀察結果就不要再讀同一個畫面。
4. 【報告風格】繁體中文，像懂技術的助理在報告，3～5 句：在做什麼、進度（例如 epoch、loss、測試通過數）、有沒有錯誤、有沒有需要使用者決定的事。
   不要貼原始畫面、不要用 Markdown 標題。狀態不明時誠實說看不出來。
5. 「幫我貼上接下來這段訊息」這類話 → arm_paste。
6. 「如果在問要不要允許就幫我按允許」→ 先 read_screen（done=false）；確認畫面真的是權限確認後，send_key key="1"，並在 reply 轉述它在問什麼。
   要拒絕用 key="esc"。bot 會先讓使用者按按鈕同意才真的送出，你不需要拒絕這類要求。
7. send_text、new_cli 會自動關注，不用另外 watch。使用者說「結束跟我說／回報給我」時，對還沒關注的 CLI 加上 watch。
8. 使用者只是聊天或問你問題（跟 CLI 無關）時，actions 給空陣列，直接在 reply 回答。
9. 路徑裡的 ~ 代表使用者的家目錄，原樣保留即可。
10. 清單裡 state 是「一般終端機（等你下指令）」的不是 claude，而是普通的 shell；送進去的文字會被當成指令執行。
   使用者明確要求在那個終端機執行某個指令時，才用 send_text（text 一樣要逐字複製使用者給的指令），bot 會先請使用者確認。
   問它在做什麼時，一樣先 read_screen 再摘要（例如指令的輸出、有沒有錯誤）。
"""

SUMMARY_SYSTEM = """你是使用者的 CLI 管家，要把某個 Claude Code CLI 剛完成的一輪工作，整理成給使用者的手機通知。
規則：繁體中文；3～5 句；像懂技術的助理在報告：做了什麼、結果、有沒有錯誤、有沒有需要使用者決定或回答的事。
如果 CLI 在最後向使用者提問或需要決定，第一句以「❓」開頭並清楚說出它在問什麼。
不要用 Markdown 標題或表格，不要貼程式碼，不要加開場白。只輸出通知內容。"""


class BrainError(Exception):
    """大腦呼叫失敗（逾時、沒登入、回傳格式不對…），訊息是給人看的中文說明。"""


@dataclass
class Plan:
    """大腦決定的行動計畫。"""

    reply: str
    done: bool
    actions: list[dict[str, Any]] = field(default_factory=list)


def extract_json(text: str) -> dict[str, Any] | None:
    """從大腦的文字回覆中找出 JSON 物件（可能被包在 ```json 區塊裡）；找不到回傳 None。"""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def to_plan(data: dict[str, Any]) -> Plan:
    """把大腦回傳的 JSON 轉成 Plan，順便過濾掉格式不對的動作。"""
    actions = [a for a in data.get("actions") or [] if isinstance(a, dict) and a.get("tool") in TOOLS]
    return Plan(reply=str(data.get("reply") or "").strip(), done=bool(data.get("done", True)), actions=actions)


class Brain:
    """呼叫 claude -p 的大腦。"""

    def __init__(self, cfg: Config) -> None:
        """記住設定，準備一個空的工作資料夾。"""
        self.cfg = cfg
        self.workdir = cfg.data_dir / "brain-workdir"
        self.last_mode = ""      # 最近一次是用 structured_output 還是從文字解析（自我檢查時顯示）
        self._safe_mode: bool | None = None   # 這個 claude 版本支援 --safe-mode 嗎？第一次呼叫時檢查

    async def _supports_safe_mode(self) -> bool:
        """檢查 claude --help 裡有沒有 --safe-mode（結果記住，只檢查一次）。"""
        if self._safe_mode is None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *shlex.split(self.cfg.brain_cmd), "--help", stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=self._env())
                out, _ = await asyncio.wait_for(proc.communicate(), 20)
                self._safe_mode = b"--safe-mode" in out
            except (OSError, asyncio.TimeoutError):
                self._safe_mode = False
            log.info("大腦%s使用 --safe-mode", "" if self._safe_mode else "不")
        return self._safe_mode

    def _env(self) -> dict[str, str]:
        """大腦子行程的環境變數：標記成大腦、移除 tmux 相關變數。"""
        env = dict(os.environ)
        env["ZUEBOT_BRAIN"] = "1"
        for key in ("TMUX", "TMUX_PANE", "CLAUDECODE"):
            env.pop(key, None)
        return env

    async def _run(self, prompt: str, system: str, model: str, schema: dict | None = None,
                   timeout: float | None = None) -> dict[str, Any]:
        """執行一次 claude -p，回傳它輸出的 JSON 結果物件；失敗丟出 BrainError。"""
        self.workdir.mkdir(parents=True, exist_ok=True)
        cmd = [*shlex.split(self.cfg.brain_cmd), "-p", "--output-format", "json", "--tools", "",
               "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
               "--disallowedTools", "mcp__*",      # 這個參數可以接好幾個值，後面一定要緊接另一個選項
               "--system-prompt", system]
        if await self._supports_safe_mode():
            cmd.append("--safe-mode")
        if model:
            cmd += ["--model", model]
        if schema:
            cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        timeout = timeout or self.cfg.brain_timeout
        log.debug("大腦 ▶ model=%s prompt=%d 字", model or "預設", len(prompt))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=str(self.workdir), env=self._env())
        except FileNotFoundError:
            raise BrainError(f"找不到 claude 指令（{self.cfg.brain_cmd}），請檢查 .env 的 CLAUDE_CMD") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BrainError(f"大腦超過 {int(timeout)} 秒沒有回應") from None

        stdout = out.decode("utf-8", "replace").strip()
        stderr = err.decode("utf-8", "replace").strip()
        try:
            data = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {"result": str(data)}
        if proc.returncode != 0 or data.get("is_error"):
            detail = str(data.get("result") or stderr or stdout or f"結束碼 {proc.returncode}")[:300]
            if re.search(r"log ?in|auth|credential|401", detail, re.IGNORECASE):
                raise BrainError(f"claude 好像沒有登入（{detail}），請在電腦上執行 claude 並 /login")
            if re.search(r"limit|quota|429|overloaded", detail, re.IGNORECASE):
                raise BrainError(f"訂閱額度或服務暫時受限：{detail}")
            raise BrainError(f"claude -p 執行失敗：{detail}")
        log.debug("大腦 ◀ %s", str(data.get("structured_output") or data.get("result"))[:500])
        return data

    async def plan(self, message: str, context: dict[str, Any], history: list[tuple[str, str]],
                   observations: list[dict[str, Any]]) -> Plan:
        """
        決定要怎麼回應你的一則訊息。
        context：目前的 CLI 清單與狀態；history：最近幾輪對話；observations：上一輪 read_screen 的結果。
        """
        parts = ["## 目前的 CLI", json.dumps(context, ensure_ascii=False, indent=1)]
        if history:
            parts += ["## 最近的對話（由舊到新）"] + [f"{role}：{text}" for role, text in history]
        if observations:
            parts += ["## 觀察結果（你上一輪要求讀的畫面）", json.dumps(observations, ensure_ascii=False, indent=1)]
        parts += ["## 使用者剛剛說", message]
        data = await self._run("\n\n".join(parts), PLAN_SYSTEM, self.cfg.brain_model, PLAN_SCHEMA)
        structured = data.get("structured_output")
        if isinstance(structured, dict):
            self.last_mode = "structured_output"
            return to_plan(structured)
        parsed = extract_json(str(data.get("result") or ""))
        if parsed is None:
            raise BrainError("大腦的回覆不是預期的格式")
        self.last_mode = "從文字解析 JSON"
        return to_plan(parsed)

    async def summarize(self, name: str, cwd: str, reply: str, screen_tail: str, situation: str) -> str:
        """把一輪工作的結果整理成 3～5 句的通知。reply／screen_tail 必須是已遮蔽過機密的內容。"""
        prompt = (f"CLI 名稱：{name}\n工作目錄：{cwd}\n情況：{situation}\n\n"
                  f"## 它最後的回覆\n{reply[-6000:] or '（沒有抓到）'}\n\n## 畫面最後幾行\n{screen_tail[-3000:]}")
        data = await self._run(prompt, SUMMARY_SYSTEM, self.cfg.summary_model, timeout=min(self.cfg.brain_timeout, 45))
        text = str(data.get("result") or "").strip()
        if not text:
            raise BrainError("大腦沒有產生摘要")
        return text

    async def ping(self) -> str:
        """自我檢查用：確認 claude -p 可以用你的訂閱回應。"""
        data = await self._run("請只回覆兩個字：正常", "你是測試助理，只回覆使用者要求的內容。",
                               self.cfg.summary_model, timeout=60)
        return str(data.get("result") or "").strip()
