# Mac 安裝教學（給初學者）

> 目標：在**被控制的那台 Mac 桌機**上裝好 zuebot。裝完之後，你人在外面也能用手機 Telegram 指揮這台 Mac 上的 Claude Code。
> 每一步都附上要打的指令和預期看到的結果。指令請在「終端機」App 裡輸入（按 `⌘ + 空白鍵`，搜尋「終端機」或「Terminal」）。

---

## 0. 開始前的準備清單

| 要準備的東西 | 說明 |
|---|---|
| Mac 桌機的登入密碼 | 安裝 Homebrew 時要輸入（輸入時畫面不會顯示字，這是正常的） |
| Claude 訂閱帳號（Pro 或 Max） | claude 和 bot 的大腦都用這個帳號，**不需要 API key** |
| 手機上的 Telegram | 用來建立 bot、之後指揮電腦 |
| 能存取 GitHub 上 `zuemen/zuebot` 的帳號 | 如果 repo 是私人的，下載時需要登入 |
| 大約 30 分鐘 | 大部分時間在等下載 |

---

## 1. 安裝 Homebrew（Mac 的套件管理工具）

先檢查有沒有裝過：
```bash
brew --version
```
- 看到 `Homebrew 4.x.x` → 已經有了，跳到第 2 步。
- 看到 `command not found` → 貼上下面這行安裝：
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
裝完後，畫面最後會出現「Next steps」，**照著它的指示執行那兩行 `echo … >> ~/.zprofile` 和 `eval …`**（Apple 晶片的 Mac 一定要做），然後關掉終端機重開，再打一次 `brew --version` 確認。

## 2. 安裝並登入 Claude Code

檢查：
```bash
claude --version
```
- 有版本號 → 已安裝。
- `command not found` → 依官方說明安裝（https://code.claude.com/docs/en/setup），例如：
```bash
curl -fsSL https://claude.ai/install.sh | bash
```
然後登入你的訂閱帳號：
```bash
claude
```
第一次會引導你選主題、登入（選「Claude account with subscription」），瀏覽器會跳出授權頁面，按同意即可。看到輸入框後輸入 `/exit` 離開。

## 3. 下載 zuebot

```bash
cd ~
git clone https://github.com/zuemen/zuebot.git
cd zuebot
```
> 如果程式還在開發分支上（還沒合併到 main），改用：
> `git clone -b claude/wizardly-gates-bdob1z https://github.com/zuemen/zuebot.git`
>
> repo 是私人的話，git 會要你登入。最簡單的方式是先 `brew install gh`，再 `gh auth login` 依指示登入 GitHub。

預期結果：`ls` 會看到 `README.md  PROMPT.md  src  scripts …`。

## 4. 一鍵安裝

```bash
./scripts/install.sh
```
它會自動完成：安裝 tmux、找到或安裝 Python 3.12、建立虛擬環境、安裝 zuebot、建立 `.env`、把 hook 加進 Claude Code 的設定（會先備份）、讓你可以直接打 `cc`。

預期結果：最後跑環境檢查，大部分是 ✅，只剩 **`TELEGRAM_BOT_TOKEN` 和 `ALLOWED_USER_IDS` 是 ❌**（下一步處理）。

## 5. 建立 Telegram bot

1. 在 Telegram 搜尋 **@BotFather**（有藍色勾勾的那個），按「開始」。
2. 傳 `/newbot`。
3. 它問名稱：隨便取，例如 `我的桌機管家`。
4. 它問 username：必須是英文、以 `bot` 結尾而且沒人用過，例如 `zue_desktop_bot`。
5. 它會回一串像 `7123456789:AAH...` 的 **token**。這串等於 bot 的密碼，**不要給別人、不要貼到任何公開的地方**。

把 token 填進設定檔：
```bash
open -e .env
```
會用「文字編輯」打開 `.env`，把 `TELEGRAM_BOT_TOKEN=` 後面換成你的 token，存檔（`⌘ + S`）。

## 6. 取得你的 user id，設定白名單

```bash
.venv/bin/python -m zuebot
```
預期看到 `Bot 啟動。允許的資料夾根目錄：…`。

在 Telegram 打開你的 bot（搜尋剛剛的 username），傳 `/start`，bot 會回：
```
未授權。你的 user id 是 123456789
```
回到終端機按 `Ctrl + C` 停掉 bot。打開 `.env`，填入：
```
ALLOWED_USER_IDS=123456789
```
存檔後再檢查一次：
```bash
./scripts/check_env.sh
```
預期最後出現 `🎉 全部通過`。

## 7. 自我檢查（用真的 claude 驗證）

```bash
.venv/bin/python -m zuebot.selftest
```
它會開一個測試用的 claude，依序測試：信任資料夾提示、單行／多行／中文／長文字貼上、Stop hook、權限確認畫面、大腦。過程約 2～4 分鐘，會用掉幾則訂閱額度。
- 遇到「要信任這個測試資料夾並繼續嗎？」→ 按 Enter。
- 結束時印出 `失敗 0 項` 就代表全部正常。
- 有失敗的話，報告存在 `~/.zuebot/selftest-report.md`。在這台 Mac 開 claude 說「請讀 ~/.zuebot/selftest-report.md，依報告修正 zuebot」即可。

## 8. 手動啟動，試用看看

```bash
.venv/bin/python -m zuebot
```
另外開一個終端機視窗（`⌘ + N`）。安裝後新開的視窗都會自動在 tmux 裡，直接打 `claude` 也會自動開在 tmux 裡：
```bash
mkdir -p ~/projects/demo && cd ~/projects/demo && claude
```
畫面會先出現「📡 zuebot：這個 claude 開在 tmux「demo」裡」，接著就是平常的 claude。
關掉視窗、或按 `Ctrl + b` 放開後再按 `d`，claude 都會在背景繼續跑；回到它：`cc demo`。
> 安裝前就開著的視窗（包括已經在跑的 claude）bot 看不到，要關掉重開。

在 Telegram 試試看：
- `現在有哪些 CLI？`
- `測試那個在幹嘛？`
- `跟測試那個說：建立一個 hello.py，印出 hello`
- `/list`、`/look 測試`（斜線指令是備用方式）

## 9. 開機自動啟動，並防止睡眠

先按 `Ctrl + C` 停掉剛剛手動開的 bot（同一個 token 只能有一個程式在收訊息），然後：
```bash
./scripts/install_launchd.sh
```
預期看到 `✅ 已安裝並啟動`。之後開機登入就會自動執行，掛掉也會自動重開。bot 會在啟動時傳一則「🤖 zuebot 已啟動」給你。
- log 位置：`~/.zuebot/logs/bot.log`（看即時 log：`tail -f ~/.zuebot/logs/bot.log`）
- 停止並移除：`./scripts/install_launchd.sh --uninstall`

**防止睡眠**：bot 執行期間會用 macOS 內建的 `caffeinate` 防止系統睡眠（螢幕還是可以關）。另外建議到「系統設定 → 能源」：
- 打開「顯示器關閉時防止自動進入睡眠」
- 打開「停電後自動開機」

**重開機後要能自動登入**：launchd 的使用者服務要等你登入後才會啟動。如果希望停電重開後也能用，要到「系統設定 → 使用者與群組 → 自動以此身分登入」選你的帳號（有開 FileVault 的話無法自動登入，只能手動登入一次）。

---

## 日常使用速查

| 你想做的事 | 在 Telegram 說 |
|---|---|
| 看有哪些 CLI | `現在有哪些 CLI？` |
| 看某個在做什麼 | `競賽那個在幹嘛？` |
| 關注、完成時通知 | `關注競賽那個，結束跟我說` |
| 轉貼一段話（原文照送） | `跟競賽那個說：改用 v2 資料集` |
| 轉貼下一則訊息（可多行） | `幫我貼上接下來這段訊息`，然後傳內容 |
| 直接打字給目前對象 | 先 `/use 競賽`，之後以 `>` 開頭，例如 `> 繼續` |
| 開新的 CLI | `開一個新的 CLI 在 ~/projects/thesis，幫我整理 related work` |
| 幫忙按允許 | `競賽那個如果在問要不要允許，就幫我按允許`（會先問你） |
| 看原始畫面 | `給我競賽那個的原文` 或 `/look 競賽` |

在電腦前：`cc 名稱 資料夾` 開新的，`cc 名稱` 回到已經開著的，`cc` 列出全部。

## 疑難排解

| 狀況 | 解法 |
|---|---|
| bot 沒反應 | `tail -50 ~/.zuebot/logs/bot.log` 看錯誤；`./scripts/check_env.sh` 逐項檢查 |
| 收到「409 Conflict」通知 | 同一個 token 有兩個 bot 在跑（例如手動開了一個，launchd 又開了一個）。關掉其中一個 |
| 完成時沒有通知 | 要先「關注」那個 CLI；`.venv/bin/python -m zuebot.setup_hooks --check` 確認 hook；改過 hook 設定後要重開 claude |
| 中文 session 名稱變成底線 | 更新到最新版 zuebot（已修正）；手動開 tmux 時加 `-u` |
| 大腦一直逾時 | 執行 `claude auth status` 確認登入；可在 `.env` 設 `BRAIN_MODEL=haiku` 換成較快的模型；斜線指令不受影響 |
| bot 說「沒有 CLI 在跑」 | 安裝前就開著的視窗看不到，關掉重開；確認 `~/.zshrc` 有 `source …/shell/zuebot.zsh` 這行，並開一個「新的」視窗 |
| 在 tmux 裡沒辦法用滑鼠選取文字 | 按住 `fn`（Terminal）或 `Option`（iTerm2）再拖曳 |
| 不想讓每個視窗都進 tmux | 在 `~/.zshrc` 的 `source …/shell/zuebot.zsh` 前一行加 `ZUEBOT_TMUX_EVERY_TERMINAL=0` |
| 想看 bot 執行了哪些 tmux 指令 | 手動啟動時加 `--debug`：`.venv/bin/python -m zuebot --debug` |
