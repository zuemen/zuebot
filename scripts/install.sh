#!/usr/bin/env bash
# install.sh —— zuebot 一鍵安裝（macOS）
#
# 用法：在專案資料夾執行  ./scripts/install.sh
# 可以重複執行：已經裝好的步驟會自動略過。
#
# 會做的事：
#   1. 檢查 Homebrew、claude
#   2. 安裝 tmux、Python 3.12（如果還沒有）
#   3. 建立 .venv 並安裝 zuebot
#   4. 建立 .env（如果還沒有），並自動填入 tmux / claude 的完整路徑
#   5. 把 hook 合併進 ~/.claude/settings.json（會先備份）
#   6. 讓你可以直接打 cc 開 CLI（加到 ~/.zshrc）
#   7. 執行環境檢查
#
# 不會做的事：不會幫你建立 Telegram bot、不會填 token（這兩件事需要你本人操作，見 docs/mac-setup.md）

set -u
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"

step() { printf '\n\033[1m▶ %s\033[0m\n' "$1"; }
ok()   { printf '  ✅ %s\n' "$1"; }
warn() { printf '  ⚠️  %s\n' "$1"; }
fail() { printf '  ❌ %s\n' "$1"; exit 1; }

# Apple 晶片的 Homebrew 在 /opt/homebrew，Intel 在 /usr/local；先把兩個都加進 PATH
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

step "1. 檢查 Homebrew"
if command -v brew >/dev/null 2>&1; then
  ok "Homebrew：$(brew --version | head -1)"
else
  echo '  請先安裝 Homebrew（需要輸入電腦密碼），在終端機貼上這行：'
  echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  fail "沒有 Homebrew，裝好後再執行一次 ./scripts/install.sh"
fi

step "2. 安裝 tmux"
if command -v tmux >/dev/null 2>&1; then
  ok "$(tmux -V)（$(command -v tmux)）"
else
  brew install tmux || fail "tmux 安裝失敗"
  ok "$(tmux -V)"
fi

step "3. 找 Python 3.10 以上"
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$candidate")"; break
  fi
done
if [ -z "$PY" ]; then
  echo "  系統內建的 Python 太舊，用 Homebrew 安裝 Python 3.12…"
  brew install python@3.12 || fail "Python 安裝失敗"
  PY="$(brew --prefix)/bin/python3.12"
fi
ok "使用 $PY（$("$PY" --version)）"

step "4. 檢查 Claude Code"
CLAUDE="$(command -v claude || true)"
if [ -z "$CLAUDE" ]; then
  echo "  找不到 claude。請先安裝 Claude Code（官方說明：https://code.claude.com/docs/en/setup），"
  echo "  例如：curl -fsSL https://claude.ai/install.sh | bash"
  fail "裝好並登入（在終端機執行 claude，輸入 /login）後再執行一次"
fi
ok "claude：$("$CLAUDE" --version 2>/dev/null)（$CLAUDE）"

step "5. 建立虛擬環境並安裝 zuebot"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv || fail "建立 .venv 失敗"
fi
.venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1
.venv/bin/python -m pip install -q -e . || fail "pip install 失敗"
ok "已安裝到 $ROOT/.venv"

step "6. 建立 .env"
if [ ! -f .env ]; then
  cp .env.example .env
  ok "已從 .env.example 複製出 .env"
else
  ok ".env 已存在，不覆蓋"
fi
# 自動填入完整路徑：launchd 開機啟動時沒有 Homebrew 的 PATH，用完整路徑最保險
.venv/bin/python - "$(command -v tmux)" "$CLAUDE" <<'PYEOF'
import re, sys
from pathlib import Path
tmux, claude = sys.argv[1], sys.argv[2]
p = Path(".env"); text = p.read_text(encoding="utf-8")
def put(text, key, value, only_if_default):
    pattern = re.compile(rf"^{key}=(.*)$", re.M)
    m = pattern.search(text)
    if m is None:
        return text + f"\n{key}={value}\n"
    if only_if_default and m.group(1).strip() not in only_if_default:
        return text   # 使用者自己改過，就不動
    return pattern.sub(f"{key}={value}", text, count=1)
text = put(text, "TMUX_BIN", tmux, {"", "tmux"})
text = put(text, "CLAUDE_CMD", claude, {"", "claude"})
p.write_text(text, encoding="utf-8")
print(f"  ✅ TMUX_BIN={tmux}\n  ✅ CLAUDE_CMD={claude}")
PYEOF
mkdir -p "$HOME/projects"

step "7. 安裝 Claude Code hook"
.venv/bin/python -m zuebot.setup_hooks || fail "hook 安裝失敗"

step "8. 讓終端機都跑在 tmux 裡（手機才看得到）、可以直接打 cc"
chmod +x bin/cc scripts/*.sh src/zuebot/hook.py
ZSHRC="$HOME/.zshrc"
if grep -q "zuebot/bin/cc" "$ZSHRC" 2>/dev/null || grep -q "$ROOT/bin/cc" "$ZSHRC" 2>/dev/null; then
  ok "~/.zshrc 已經有 cc 的設定"
else
  {
    echo ""
    echo "# zuebot：用 tmux 開一個 claude，讓 Telegram bot 管得到它（cc <名稱> [資料夾]）"
    echo "alias cc=\"$ROOT/bin/cc\""
  } >> "$ZSHRC"
  ok "已加入 cc 指令"
fi
if grep -q "shell/zuebot.zsh" "$ZSHRC" 2>/dev/null; then
  ok "~/.zshrc 已經有自動 tmux 的設定"
else
  {
    echo ""
    echo "# zuebot：直接打 claude、或新開終端機視窗，都會自動放進 tmux，手機上的 bot 才看得到、管得到"
    echo "# 只想包 claude、一般視窗維持原樣：在下一行前面加 ZUEBOT_TMUX_EVERY_TERMINAL=0；全部關閉：ZUEBOT_WRAP=0"
    echo "source \"$ROOT/shell/zuebot.zsh\""
  } >> "$ZSHRC"
  ok "已加入自動 tmux（新開的終端機視窗才會生效；已經開著的視窗要關掉重開）"
fi

step "9. 環境檢查"
./scripts/check_env.sh

cat <<EOF

────────────────────────────────────────
安裝完成！接下來需要你本人做的事（詳見 docs/mac-setup.md）：
  1. 在 Telegram 找 @BotFather 建 bot，把 token 填進 $ROOT/.env 的 TELEGRAM_BOT_TOKEN
  2. 啟動 bot 取得你的 user id：.venv/bin/python -m zuebot
     （在 Telegram 傳任何訊息給 bot，它會回你的 user id；填進 .env 的 ALLOWED_USER_IDS 後重開）
  3. 自我檢查：.venv/bin/python -m zuebot.selftest
  4. 開機自動啟動：./scripts/install_launchd.sh
────────────────────────────────────────
EOF
