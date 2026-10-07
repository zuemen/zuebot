#!/usr/bin/env bash
# check_env.sh —— 逐項檢查 zuebot 需要的環境，印出 ✅／❌
#
# 用法：在專案資料夾執行  ./scripts/check_env.sh
# 全部 ✅ 時結束碼為 0；有任何 ❌ 時結束碼為 1。

cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
FAILED=0
ok()   { printf '✅ %s\n' "$1"; }
bad()  { printf '❌ %s\n' "$1"; FAILED=1; }
info() { printf 'ℹ️  %s\n' "$1"; }

# 讀 .env 裡某個鍵的值（只做最基本的解析，夠檢查用）
env_value() {
  [ -f .env ] || return 0
  grep -E "^(export )?$1=" .env | tail -1 | sed -E "s/^(export )?$1=//; s/^[\"']//; s/[\"']$//; s/[[:space:]]+#.*$//"
}

echo "── 系統 ──"
if [ "$(uname)" = "Darwin" ]; then ok "macOS $(sw_vers -productVersion 2>/dev/null)"; else info "不是 macOS（$(uname)），部分說明不適用"; fi

TMUX_BIN="$(env_value TMUX_BIN)"; TMUX_BIN="${TMUX_BIN:-tmux}"
if command -v "$TMUX_BIN" >/dev/null 2>&1; then
  ok "tmux：$("$TMUX_BIN" -V)（$(command -v "$TMUX_BIN")）"
else
  bad "找不到 tmux（$TMUX_BIN）：brew install tmux"
fi

CLAUDE_CMD="$(env_value CLAUDE_CMD)"; CLAUDE_CMD="${CLAUDE_CMD:-claude}"
if command -v "$CLAUDE_CMD" >/dev/null 2>&1; then
  ok "claude：$("$CLAUDE_CMD" --version 2>/dev/null)（$(command -v "$CLAUDE_CMD")）"
  AUTH="$("$CLAUDE_CMD" auth status --text 2>/dev/null | head -3 | tr '\n' ' ')"
  if [ -n "$AUTH" ]; then info "claude 登入狀態：$AUTH"; fi
else
  bad "找不到 claude（$CLAUDE_CMD）：請安裝 Claude Code 並登入"
fi

echo "── Python ──"
if [ -x .venv/bin/python ]; then
  if .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    ok ".venv：$(.venv/bin/python --version)"
  else
    bad ".venv 的 Python 太舊（需要 3.10 以上）：刪掉 .venv 後重新執行 ./scripts/install.sh"
  fi
  if .venv/bin/python -c 'import zuebot, telegram' 2>/dev/null; then
    ok "zuebot 與 python-telegram-bot 已安裝"
  else
    bad "套件沒裝好：.venv/bin/pip install -e ."
  fi
else
  bad "沒有 .venv：執行 ./scripts/install.sh"
fi

echo "── 設定檔 .env ──"
if [ -f .env ]; then
  ok ".env 存在"
  TOKEN="$(env_value TELEGRAM_BOT_TOKEN)"
  if [ -z "$TOKEN" ] || echo "$TOKEN" | grep -q "換成"; then
    bad "TELEGRAM_BOT_TOKEN 還沒填（向 @BotFather 申請）"
  elif echo "$TOKEN" | grep -Eq '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$'; then
    ok "TELEGRAM_BOT_TOKEN 格式正確"
  else
    bad "TELEGRAM_BOT_TOKEN 格式看起來不對（應該像 123456789:AAxx…）"
  fi
  IDS="$(env_value ALLOWED_USER_IDS)"
  if [ -z "$IDS" ]; then
    bad "ALLOWED_USER_IDS 還沒填（啟動 bot 後傳訊息給它，它會回你的 user id）"
  elif echo "$IDS" | grep -Eq '^[0-9, ]+$'; then
    ok "ALLOWED_USER_IDS=$IDS"
  else
    bad "ALLOWED_USER_IDS 只能是數字和逗號：$IDS"
  fi
  ROOT_DIR="$(env_value ALLOWED_ROOT)"; ROOT_DIR="${ROOT_DIR/#\~/$HOME}"
  if [ -z "$ROOT_DIR" ] || [ -d "$ROOT_DIR" ]; then ok "ALLOWED_ROOT=${ROOT_DIR:-（預設：家目錄）}"; else bad "ALLOWED_ROOT 資料夾不存在：$ROOT_DIR（mkdir -p $ROOT_DIR）"; fi
else
  bad "沒有 .env：cp .env.example .env 後填入設定"
fi

echo "── Claude Code hook ──"
if [ -x .venv/bin/python ]; then
  .venv/bin/python -m zuebot.setup_hooks --check | sed 's/^/   /'
  if .venv/bin/python -m zuebot.setup_hooks --check >/dev/null 2>&1; then
    ok "hook 已設定"
  else
    bad "hook 沒有完整設定：.venv/bin/python -m zuebot.setup_hooks"
  fi
else
  bad "無法檢查 hook（還沒有 .venv）：先執行 ./scripts/install.sh"
fi
DATA_DIR="$(env_value ZUEBOT_DATA_DIR)"; DATA_DIR="${DATA_DIR:-$HOME/.zuebot}"; DATA_DIR="${DATA_DIR/#\~/$HOME}"
if mkdir -p "$DATA_DIR" 2>/dev/null && [ -w "$DATA_DIR" ]; then ok "資料夾可寫入：$DATA_DIR"; else bad "無法寫入 $DATA_DIR"; fi

echo "── 常駐與睡眠 ──"
if [ -f "$HOME/Library/LaunchAgents/com.zuebot.bot.plist" ]; then
  if launchctl print "gui/$(id -u)/com.zuebot.bot" >/dev/null 2>&1; then ok "launchd 開機自動啟動：已安裝並載入"; else info "launchd plist 存在但沒有載入：./scripts/install_launchd.sh"; fi
else
  info "還沒設定開機自動啟動（之後執行 ./scripts/install_launchd.sh）"
fi
if [ "$(uname)" = "Darwin" ]; then
  SLEEP="$(pmset -g 2>/dev/null | awk '$1=="sleep"{print $2}')"
  if [ "$SLEEP" = "0" ]; then ok "系統不會自動睡眠"; else info "系統 ${SLEEP:-?} 分鐘後會睡眠；launchd 版本會用 caffeinate 防止睡眠，或見 docs/mac-setup.md"; fi
fi

echo
if [ "$FAILED" = 0 ]; then echo "🎉 全部通過"; else echo "有 ❌ 項目，依提示修正後再執行一次 ./scripts/check_env.sh"; fi
exit "$FAILED"
