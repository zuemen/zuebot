"""測試 safety.py：危險字眼偵測與機密遮蔽，以及 screen.menu_options。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zuebot import safety, screen  # noqa: E402


class TestDanger(unittest.TestCase):
    """危險字眼：寧可多擋，但常見的一般字詞不能誤判。"""

    def test_detects(self):
        """PROMPT.md 第 6 節列出的字眼都要抓到。"""
        cases = {
            "rm -rf build": "rm", "請rm掉暫存檔": "rm", "幫我刪除 logs": "刪除", "git push origin main": "git push",
            "git push --force": "--force", "git reset --hard HEAD~1": "reset --hard", "部署到正式機": "部署",
            "DROP TABLE users;": "drop", "sudo apt install": "sudo", "把資料夾清空": "清空",
        }
        for text, expected in cases.items():
            self.assertIn(expected, safety.find_danger(text), text)

    def test_no_false_positive(self):
        """一般句子不能被當成危險（confirm、format、program 裡有 rm 的字母）。"""
        for text in ["請 confirm 一下", "改用 v2 資料集", "跑第 3 個 epoch", "information", "幫我整理 related work",
                     "dropdown 選單", "inform the user"]:
            self.assertEqual(safety.find_danger(text), [], text)

    def test_extra_words(self):
        """.env 自訂的字眼也要生效。"""
        self.assertEqual(safety.find_danger("上線到 production", ("production",)), ["production"])


class TestMask(unittest.TestCase):
    """機密遮蔽。"""

    def test_tokens(self):
        """各種金鑰格式都要遮蔽，只留開頭。"""
        secrets = [
            "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "AKIAABCDEFGHIJKLMNOP",
            "7123456789:AAHabcdefghijklmnopqrstuvwxyz0123456", "xoxb-1234567890-abcdefghij",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstu",
        ]
        for secret in secrets:
            masked = safety.mask_secrets(f"key: {secret} end")
            self.assertNotIn(secret, masked, secret)
            self.assertIn(safety.MASK, masked)

    def test_env_and_private_key(self):
        """.env 內容與私鑰區塊。"""
        env = "DATABASE_URL=postgres://x\nOPENAI_API_KEY=abcd1234efgh5678\nexport MY_SECRET='topsecretvalue'\nDEBUG=1"
        masked = safety.mask_secrets(env)
        self.assertNotIn("abcd1234efgh5678", masked)
        self.assertNotIn("topsecretvalue", masked)
        self.assertIn("DEBUG=1", masked)
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OPENSSH PRIVATE KEY-----"
        self.assertNotIn("b3BlbnNzaC1", safety.mask_secrets(f"內容：\n{key}\n結束"))

    def test_json_and_bearer(self):
        """JSON 裡的 api_key、HTTP 標頭的 Bearer token。"""
        masked = safety.mask_secrets('{"api_key": "abcdef123456", "name": "demo"}\nAuthorization: Bearer abcdefghijklmnop1234')
        self.assertNotIn("abcdef123456", masked)
        self.assertNotIn("abcdefghijklmnop1234", masked)
        self.assertIn('"name": "demo"', masked)

    def test_normal_text_untouched(self):
        """一般文字不能被改動（中文、git hash、路徑）。"""
        text = "訓練完成，loss 0.41。commit 3f2a9c1 已建立，檔案在 /Users/me/projects/demo/hello.py"
        self.assertEqual(safety.mask_secrets(text), text)


class TestMenuOptions(unittest.TestCase):
    """權限選單選項判讀（用來產生按鈕）。"""

    def test_options(self):
        """讀出 1/2/3 選項。"""
        text = ("│ Do you want to proceed?            │\n│ ❯ 1. Yes                             │\n"
                "│   2. Yes, and don't ask again for git commands │\n│   3. No, and tell Claude what to do differently (esc) │")
        options = screen.menu_options(text)
        self.assertEqual([k for k, _ in options], ["1", "2", "3"])
        self.assertEqual(options[0][1], "Yes")
        self.assertTrue(options[2][1].startswith("No"))


if __name__ == "__main__":
    unittest.main()
