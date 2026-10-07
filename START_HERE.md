# 🏠 回家後從這裡開始

> 目標：在 **Mac 桌機**上裝好 zuebot，之後你人在外面，也能用手機 Telegram 指揮這台 Mac 上的 Claude Code。
> 大部分步驟可以交給 Mac 上的 Claude Code 做，只有幾件事必須你本人動手（下面標 👤）。

---

## 1. 出門前／回家前先準備好

| | 要準備的 | 為什麼 |
|---|---|---|
| ☐ | Mac 桌機的登入密碼 | 安裝 Homebrew 時要輸入 |
| ☐ | Mac 上已經裝好 **Claude Code 並登入你的訂閱**（在終端機打 `claude` 能用） | claude 本身和 bot 的「大腦」都用你的訂閱，不需要 API key |
| ☐ | 手機上的 **Telegram** | 用來建立 bot、之後指揮電腦 |
| ☐ | Mac 能登入 GitHub（repo 是私人的話才需要） | 下載程式用 |
| ☐ | 約 30 分鐘 | 大部分時間在等下載 |

---

## 2. 👤 你本人先做這三步（在 Mac 的「終端機」App）

**① 安裝 Homebrew**（打過 `brew --version` 有版本號就跳過）。這一步要輸入密碼，Claude Code 沒辦法代勞：
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
裝完照畫面最後「Next steps」的兩行指令做，然後關掉終端機再重開。

**② 下載 zuebot**（程式目前在開發分支上）：
```bash
cd ~
git clone -b claude/wizardly-gates-bdob1z https://github.com/zuemen/zuebot.git
cd zuebot
```
> 之後如果合併到 main 了，就不用加 `-b claude/wizardly-gates-bdob1z`。
> 要求登入 GitHub 時：`brew install gh && gh auth login`，照指示登入後再 clone 一次。

**③ 在 zuebot 資料夾裡打開 Claude Code**：
```bash
claude
```

---

## 3. 對 Mac 上的 Claude Code 說這段話（直接複製貼上）

```
請讀 START_HERE.md 和 docs/mac-setup.md，幫我在這台 Mac 上安裝 zuebot：
1. 執行 ./scripts/install.sh，把有問題的地方修好（Homebrew 我已經裝了）。
2. 停下來，告訴我怎麼在 Telegram 建 bot、怎麼填 .env。token 我自己填進 .env，不要叫我貼給你。
3. 我說「填好了」之後，執行 ./scripts/check_env.sh 確認全部 ✅。
4. 執行 .venv/bin/python -m zuebot.selftest --yes，看 ~/.zuebot/selftest-report.md。
   有失敗項目就依報告修正程式碼（遵守 CLAUDE.md 的規則），修完重跑到沒有失敗為止，並 commit。
5. 最後執行 ./scripts/install_launchd.sh 設定開機自動啟動。
```

Claude Code 做到第 2 步時，會請你做下面兩件事。

---

## 4. 👤 你本人要做的：建 Telegram bot、填設定

**建 bot（用手機就行）**
1. Telegram 搜尋 **@BotFather**（有藍勾勾），按「開始」，傳 `/newbot`。
2. 取名，例如 `我的桌機管家`；username 必須以 `bot` 結尾，例如 `zue_desktop_bot`。
3. 它會回一串 `7123456789:AAH…` 的 **token**。這等於 bot 的密碼，**不要貼給任何人，也不要貼進聊天視窗**。

**填 token**：在 Mac 的終端機打 `open -e ~/zuebot/.env`，把 token 貼在 `TELEGRAM_BOT_TOKEN=` 後面，存檔。

**取得你的 user id**
```bash
cd ~/zuebot && .venv/bin/python -m zuebot
```
在 Telegram 打開你的 bot，傳 `/start`，它會回「未授權。你的 user id 是 123456789」。
回到終端機按 `Ctrl + C` 停掉 bot，把這個數字填進 `.env` 的 `ALLOWED_USER_IDS=`，存檔。
然後告訴 Claude Code「填好了」。

---

## 5. 驗收：在 Telegram 試這些（對照 PROMPT.md 第 10 節）

先在 Mac 的終端機開一個測試用 CLI：`mkdir -p ~/projects/demo && cc 測試 ~/projects/demo`。
進到 claude 畫面後，按 `Ctrl + b` 再按 `d` 離開，它會在背景繼續跑。

| # | 在 Telegram 說 | 應該看到 |
|---|---|---|
| 1 | `現在有哪些 CLI？` | 列出「測試」 |
| 2 | `測試那個在幹嘛？` | 幾句話的摘要，不是一大段終端畫面 |
| 3 | `幫我貼上接下來這段訊息到測試`，再傳一段**多行**文字 | 「已送到」；CLI 收到完整內容，只送一次 |
| 4 | `關注測試那個，結束跟我說` | CLI 完成後，10 秒內收到 ✅ 摘要 |
| 5 | `開一個新的 CLI 在 ~/projects/demo2，幫我建立一個 hello.py` | 問你信任資料夾 → 按同意 → 任務被執行 → 完成時回報 |
| 6 | CLI 跳出權限確認時 | 收到 🔔 和按鈕；按「✅ 1. Yes」後 CLI 繼續 |
| 7 | `幫我在測試那個執行 git push --force` | 先跳「⚠️ 需要你確認」，不會直接送出 |
| 8 | 用別人的 Telegram 帳號傳訊息給 bot | 只回「未授權」 |
| 9 | 再開一個 `cc 測試2`，然後說 `關注測試那個` | 列出「測試／測試2」讓你選 |
| 10 | 重開機（或 `./scripts/install_launchd.sh --restart`） | 收到「🤖 zuebot 已啟動。關注中：…」 |

有任何一項不對，把現象告訴 Mac 上的 Claude Code，請它對照 `PROMPT.md` 修正。

---

## 6. 常用指令速查

| 在哪裡 | 指令 | 做什麼 |
|---|---|---|
| Mac 終端機 | `cc 名稱 資料夾` | 開一個被管理的 claude（`cc 名稱` 回到已經開著的；`cc` 列出全部） |
| Mac 終端機 | `Ctrl + b` 再按 `d` | 離開 claude 畫面，讓它在背景繼續跑 |
| Mac 終端機 | `./scripts/install_launchd.sh --status` | 看 bot 有沒有在跑 |
| Mac 終端機 | `tail -f ~/.zuebot/logs/bot.log` | 看 bot 的即時 log |
| Telegram | 用口語說就好；`/help` 看說明 | 斜線指令是備用，大腦出問題時一樣能用 |
| Telegram | `> 文字` | 原文直接送進「目前對象」，不經過大腦 |

更詳細的說明與疑難排解：[`docs/mac-setup.md`](docs/mac-setup.md)
