# zuebot 專案說明（給 Claude Code）

- 完整需求、已定案的決策與任務清單都在 **`PROMPT.md`**，開始任何工作前先讀它。
- 在 Mac 上安裝、驗收：照 **`START_HERE.md`**；實機自我檢查的結果在 `~/.zuebot/selftest-report.md`。
- `reference/v0/` 是已驗證可運作的第一版，當作對照，不要修改。
- 一律用**繁體中文**和我溝通、寫註解、寫 commit 訊息。
- 程式要完整可執行，每個函式都要有中文註解；文件要寫給初學者看。
- 所有 tmux 操作只能出現在 `tmux_ops.py`（為了之後支援遠端多台電腦）。
- 大腦（LLM）只能透過工具層行動，不能拿到不受限的 shell。
- hook 腳本絕對不能讓 Claude 卡住：例外全吞、不輸出、結束碼 0。
- 每完成一個 Phase 就 commit，並在 `PROMPT.md` 第 7 節勾選完成項目。
