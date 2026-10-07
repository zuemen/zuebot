"""
__main__.py —— 啟動入口：`python -m zuebot`

流程：讀 .env → 設定 log → 檢查設定 → 啟動 Telegram bot。

用法：
  python -m zuebot                 一般啟動
  python -m zuebot --debug         除錯模式：每個 tmux 指令和 hook 事件都會印到 log
  python -m zuebot --env-file 路徑  指定別的 .env 檔
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import config, tmux_ops


def setup_logging(debug: bool) -> None:
    """設定 log 格式與等級。httpx 每次 polling 都會印一行，太吵，所以調成只印警告。"""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not debug:
        logging.getLogger("telegram").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    """解析命令列參數並啟動 bot。設定有誤時印出中文說明並回傳結束碼 1。"""
    parser = argparse.ArgumentParser(prog="python -m zuebot", description="用 Telegram 指揮 Claude Code CLI 的管家 bot")
    parser.add_argument("--debug", action="store_true", help="除錯模式：印出每個 tmux 指令與 hook 事件")
    parser.add_argument("--env-file", help="指定 .env 檔的位置（預設依序找目前資料夾、專案根目錄）")
    args = parser.parse_args(argv)

    setup_logging(args.debug)
    log = logging.getLogger("zuebot")
    try:
        env_file = config.load_dotenv(args.env_file)
        cfg = config.load_config()
    except config.ConfigError as e:
        print(f"❌ 設定錯誤：{e}", file=sys.stderr)
        return 1
    log.info("設定檔：%s", env_file or "（沒有找到 .env，只用環境變數）")

    tmux_ops.configure(cfg.tmux_bin)

    from . import bot   # 放在這裡才 import：設定錯誤時不需要先載入 telegram 套件
    bot.run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
