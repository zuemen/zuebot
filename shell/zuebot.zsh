# zuebot.zsh —— 讓這台 Mac 的終端機都跑在 tmux 裡，Telegram bot 才看得到、管得到
#
# 為什麼需要：一般的終端機視窗，外部程式讀不到畫面、也打不進字；
#             放在 tmux 裡，bot 才能用 tmux 讀畫面、幫你打字（PROMPT.md 第 2 節）。
#
# 由 scripts/install.sh 加進 ~/.zshrc（一行 source）。裝好之後：
#   1. 直接打 claude：自動開在一個新的 tmux session 裡（名稱＝資料夾名稱），用起來跟原本一樣。
#      關掉視窗它也會在背景繼續跑；回到它：cc 名稱
#   2. 每個新開的終端機視窗（Terminal、iTerm2）都自動在 tmux 裡，名稱 term、term-2…
#      關掉視窗，那個 session 也跟著結束（不會越積越多）。
#
# 調整方式：在 ~/.zshrc 裡 source 這個檔案的那一行「前面」加上
#   ZUEBOT_TMUX_EVERY_TERMINAL=0   只包 claude，一般終端機視窗維持原樣
#   ZUEBOT_WRAP=0                  全部關閉（打 claude 就是原本的 claude）
#
# 已經開著、沒有在 tmux 裡的視窗（包括正在跑的 claude）沒辦法事後接管，要關掉重開才會生效。

[[ -o interactive ]] || return 0
: ${ZUEBOT_WRAP:=1}
: ${ZUEBOT_TMUX_EVERY_TERMINAL:=1}
[[ "$ZUEBOT_WRAP" == 1 ]] || return 0
command -v tmux >/dev/null 2>&1 || return 0

# 開出來的 claude 不能帶著 Claude Code 自己的環境變數，否則會被當成巢狀執行而拒絕啟動
_ZUEBOT_UNSET_ENV="env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_SESSION_ID"

# 執行 tmux（固定加 -u：強制 UTF-8，中文名稱才不會變成底線）
_zuebot_tmux() {
  command tmux -u "$@"
}

# 依照 base 產生一個還沒被用過、合法的 session 名稱（空白、冒號、句點等換成 -，最多 24 字）
_zuebot_unique_name() {
  local base="${1//[^[:alnum:]_-]/-}"
  base="${base[1,24]}"
  [[ -z "$base" ]] && base="cli"
  local name="$base" i=2
  while _zuebot_tmux has-session -t "=$name" 2>/dev/null; do
    name="$base-$i"
    (( i++ ))
  done
  print -r -- "$name"
}

# 取代 claude 指令：不在 tmux 裡時，自動開一個 tmux session 來跑 claude
claude() {
  # 已經在 tmux 裡、在 Claude Code 裡、或不是在真的終端機前（例如被腳本呼叫）→ 直接執行原本的 claude
  if [[ -n "$TMUX" || -n "$CLAUDECODE" || ! -t 0 || ! -t 1 ]]; then
    command claude "$@"
    return
  fi
  local real name args
  real="$(whence -p claude)"
  if [[ -z "$real" ]]; then
    print -u2 "找不到 claude 指令"
    return 127
  fi
  name="$(_zuebot_unique_name "${PWD:t}")"
  args="${(j: :)${(q)@}}"
  print -r -- "📡 zuebot：這個 claude 開在 tmux「$name」裡，手機管得到。關掉視窗它也會繼續跑，回來用：cc $name"
  _zuebot_tmux new-session -s "$name" -c "$PWD" "$_ZUEBOT_UNSET_ENV ${(q)real} $args" \; set-option mouse on
}

# 每個新開的終端機視窗自動進 tmux（只限 Terminal 和 iTerm2 的一般視窗；ssh 連進來、Claude Code 裡面都不動）
if [[ "$ZUEBOT_TMUX_EVERY_TERMINAL" == 1 && -z "$TMUX" && -z "$CLAUDECODE" && -z "$SSH_CONNECTION" \
      && ( "$TERM_PROGRAM" == "Apple_Terminal" || "$TERM_PROGRAM" == "iTerm.app" ) ]]; then
  # destroy-unattached：關掉視窗時這個 session 也結束；mouse：觸控板捲動可以看歷史
  # tmux 正常結束（你在裡面打 exit）就把外層視窗也關掉；tmux 啟動失敗則留在原本的 shell，不會把你鎖在外面
  _zuebot_tmux new-session -s "$(_zuebot_unique_name term)" \; set-option destroy-unattached on \; set-option mouse on \
    && exit
fi
