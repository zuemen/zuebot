"""
config.py —— 讀取設定

設定一律來自環境變數；為了方便，啟動時會先讀 `.env` 檔，把裡面的值放進環境變數
（已經存在的環境變數不會被覆蓋，所以你在終端機臨時 export 的值優先）。

這裡自己寫了一個很小的 .env 解析器，而不是安裝 python-dotenv，
因為規格要求「能用標準函式庫就不要加套件」。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 專案根目錄（src/zuebot/config.py 往上兩層）。用 `pip install -e .` 安裝時這裡就是 repo 根目錄。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 事件檔與狀態檔的預設資料夾。hook.py 也使用同樣的預設值，兩邊才找得到彼此。
DEFAULT_DATA_DIR = Path.home() / ".zuebot"


class ConfigError(Exception):
    """設定有誤（例如缺少 bot token）時丟出，訊息是給人看的中文說明。"""


@dataclass(frozen=True)
class Config:
    """整個 bot 用到的設定，建立後不可修改，避免執行中被意外改掉。"""

    bot_token: str                    # BotFather 給的 token
    allowed_user_ids: frozenset[int]  # 白名單：只有這些 Telegram user id 能操作
    allowed_root: Path                # /new 只能在這個資料夾底下開 CLI
    claude_cmd: str                   # 開新 CLI 時在 tmux 裡執行的指令
    tmux_bin: str                     # tmux 執行檔（launchd 沒有 Homebrew 的 PATH 時可填完整路徑）
    data_dir: Path                    # 狀態檔、事件檔放這裡
    events_file: Path                 # hook 寫入、bot 讀取的事件檔
    state_file: Path                  # bot 狀態存檔


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """
    解析 .env 的一行，回傳 (鍵, 值)；空行、註解或格式不對就回傳 None。

    支援的寫法：
      KEY=value
      export KEY=value
      KEY="有 空白 的值"      （引號內的 # 不算註解）
      KEY=value  # 行尾註解
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()
    if not key:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]           # 去掉成對的引號
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()   # 去掉行尾註解
    return key, value


def load_dotenv(path: str | Path | None = None) -> Path | None:
    """
    找到 .env 並載入到 os.environ，回傳實際讀到的檔案路徑（找不到就回傳 None）。

    尋找順序：
      1. 參數指定的路徑（命令列 --env-file）
      2. 環境變數 ZUEBOT_ENV_FILE
      3. 目前所在資料夾的 .env
      4. 專案根目錄的 .env（讓 launchd 從任何位置啟動都找得到）
    """
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path).expanduser())
    if os.environ.get("ZUEBOT_ENV_FILE"):
        candidates.append(Path(os.environ["ZUEBOT_ENV_FILE"]).expanduser())
    candidates += [Path.cwd() / ".env", PROJECT_ROOT / ".env"]

    for candidate in candidates:
        if candidate.is_file():
            for raw in candidate.read_text(encoding="utf-8").splitlines():
                parsed = _parse_env_line(raw)
                if parsed:
                    os.environ.setdefault(parsed[0], parsed[1])   # 已存在的環境變數優先
            return candidate
    if path:
        raise ConfigError(f"找不到指定的設定檔：{path}")
    return None


def _parse_user_ids(raw: str) -> frozenset[int]:
    """把 "123,456" 轉成 {123, 456}；有非數字就丟出 ConfigError 說明哪一個錯了。"""
    ids = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ConfigError(f"ALLOWED_USER_IDS 裡的「{part}」不是數字。格式範例：123456789,987654321")
        ids.add(int(part))
    return frozenset(ids)


def load_config() -> Config:
    """從環境變數組出 Config；缺少必要設定時丟出 ConfigError。"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("還沒設定 TELEGRAM_BOT_TOKEN。請複製 .env.example 成 .env，填入 BotFather 給你的 token。")

    allowed_root = Path(os.environ.get("ALLOWED_ROOT", str(Path.home()))).expanduser().resolve()
    if not allowed_root.is_dir():
        raise ConfigError(f"ALLOWED_ROOT 指定的資料夾不存在：{allowed_root}")

    data_dir = Path(os.environ.get("ZUEBOT_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
    events_file = Path(os.environ.get("ZUEBOT_EVENTS", str(data_dir / "events.jsonl"))).expanduser()

    return Config(
        bot_token=token,
        allowed_user_ids=_parse_user_ids(os.environ.get("ALLOWED_USER_IDS", "")),
        allowed_root=allowed_root,
        claude_cmd=os.environ.get("CLAUDE_CMD", "claude").strip() or "claude",
        tmux_bin=os.environ.get("TMUX_BIN", "tmux").strip() or "tmux",
        data_dir=data_dir,
        events_file=events_file,
        state_file=data_dir / "bot_state.json",
    )
