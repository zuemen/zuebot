#!/usr/bin/env bash
# install_launchd.sh —— 讓 zuebot 在 macOS 登入後自動啟動、掛掉自動重開
#
# 用法：
#   ./scripts/install_launchd.sh              安裝並啟動（重複執行＝更新並重開）
#   ./scripts/install_launchd.sh --uninstall  停止並移除
#   ./scripts/install_launchd.sh --restart    重開（改過 .env 之後用）
#   ./scripts/install_launchd.sh --status     看目前狀態
#
# 注意：同一個 bot token 只能有一個程式在收訊息。安裝前請先關掉手動開的 bot（按 Ctrl-C）。

set -u
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
LABEL="com.zuebot.bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOGDIR="$HOME/.zuebot/logs"

case "${1:-}" in
  --uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && echo "⏹ 已停止"
    rm -f "$PLIST" && echo "🗑 已移除 $PLIST"
    exit 0 ;;
  --restart)
    launchctl kickstart -k "$DOMAIN/$LABEL" && echo "🔄 已重開" || echo "❌ 重開失敗：還沒安裝嗎？執行 ./scripts/install_launchd.sh"
    exit 0 ;;
  --status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl print "$DOMAIN/$LABEL" | grep -E "state =|pid =|last exit code" | sed 's/^[[:space:]]*/  /'
      echo "  最近的 log："; tail -5 "$LOGDIR/bot.log" 2>/dev/null | sed 's/^/    /'
    else
      echo "還沒安裝（或沒有載入）"
    fi
    exit 0 ;;
esac

if [ "$(uname)" != "Darwin" ]; then
  echo "❌ launchd 只有 macOS 才有"; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "❌ 找不到 .venv，請先執行 ./scripts/install.sh"; exit 1
fi
echo "▶ 先檢查設定"
if ! ./scripts/check_env.sh >/dev/null; then
  echo "⚠️  check_env 有 ❌ 項目（執行 ./scripts/check_env.sh 看詳情）。bot 可能啟動失敗，仍繼續安裝。"
fi
if pgrep -f "python.* -m zuebot( |$)" >/dev/null 2>&1 && ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "⚠️  偵測到有手動開啟的 zuebot 正在執行。請先到那個終端機按 Ctrl-C，否則會出現 409 Conflict。"
  read -r -p "已經關掉了，繼續安裝？[y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ] || exit 1
fi

PYTHON="$ROOT/.venv/bin/python"
# PATH：Homebrew（Apple 晶片與 Intel）、使用者自己的 bin、claude 和 tmux 所在的資料夾、系統預設
EXTRA=""
for cmd in claude tmux; do
  p="$(command -v "$cmd" 2>/dev/null || true)"
  [ -n "$p" ] && EXTRA="$EXTRA:$(dirname "$p")"
done
PATH_VALUE="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin${EXTRA}:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__ROOT__#$ROOT#g" -e "s#__PATH__#$PATH_VALUE#g" -e "s#__LOGDIR__#$LOGDIR#g" \
  config/com.zuebot.bot.plist.template > "$PLIST"
plutil -lint "$PLIST" >/dev/null || { echo "❌ 產生的 plist 格式有誤：$PLIST"; exit 1; }

# 已經裝過就先停掉舊的。bootout 是在背景完成的，要等它真的卸載後才能重新載入，
# 否則 bootstrap 會失敗（Bootstrap failed: 5: Input/output error）
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
    sleep 1
  done
fi
# RunAtLoad 會在載入時自動啟動，不需要再 kickstart（再 kickstart 會多開一次，造成 409 Conflict）
launchctl bootstrap "$DOMAIN" "$PLIST" || { echo "❌ 載入失敗，稍等幾秒再執行一次"; exit 1; }
sleep 3
if launchctl print "$DOMAIN/$LABEL" | grep -q "state = running"; then
  echo "✅ 已安裝並啟動。Telegram 應該會收到「🤖 zuebot 已啟動」。"
else
  echo "⚠️  已安裝，但目前沒有在執行。看錯誤：tail -30 $LOGDIR/bot.log $LOGDIR/launchd.err.log"
fi
echo "   log：tail -f $LOGDIR/bot.log"
echo "   狀態：./scripts/install_launchd.sh --status　重開：--restart　移除：--uninstall"
