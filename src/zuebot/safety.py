"""
safety.py —— 安全檢查：危險字眼偵測、機密遮蔽（PROMPT.md 第 6 節）

  find_danger(text)   找出訊息裡可能造成破壞的字眼（rm、刪除、git push、--force …），
                      有的話 bot 會先跳確認按鈕，不會直接送出。
  mask_secrets(text)  把畫面或回覆裡疑似 API key、token、私鑰、.env 內容的字串遮起來，
                      再傳到 Telegram 或交給大腦。

這兩個都是「寧可多擋」的設計：誤判只會多問你一次，漏判才是真的危險。
"""

from __future__ import annotations

import re

MASK = "…[已遮蔽]"


def _ascii_word(word: str) -> str:
    """
    英文字詞的比對規則：前後不能緊接英數字（避免 confirm 裡的 rm 被誤判）。
    不用 \\b 是因為 Python 會把中文也當成「字」，「請rm掉」這種寫法 \\b 會比對不到。
    """
    return rf"(?<![A-Za-z0-9_]){word}(?![A-Za-z0-9_])"


# (顯示名稱, 正規表示式)。英文不分大小寫。
DANGER_PATTERNS: list[tuple[str, re.Pattern]] = [
    (label, re.compile(pattern, re.IGNORECASE)) for label, pattern in [
        ("rm", _ascii_word(r"rm")),
        ("rmdir", _ascii_word(r"rmdir")),
        ("git push", r"git\s+push"),
        ("--force", r"--force\b|(?<![A-Za-z0-9_])push\s+-f\b|force[\s-]push"),
        ("reset --hard", r"reset\s+--hard"),
        ("git clean", r"git\s+clean"),
        ("drop", _ascii_word(r"drop")),
        ("truncate", _ascii_word(r"truncate")),
        ("delete", _ascii_word(r"delete")),
        ("deploy", _ascii_word(r"deploy")),
        ("sudo", _ascii_word(r"sudo")),
        ("mkfs", _ascii_word(r"mkfs")),
        ("dd if=", r"(?<![A-Za-z0-9_])dd\s+if="),
        ("chmod/chown -R", r"(chmod|chown)\s+-R"),
        ("kill -9", r"kill\s+-9"),
        ("shutdown/reboot", _ascii_word(r"(shutdown|reboot)")),
        ("刪除", r"刪除|删除|刪掉|删掉"),
        ("部署", r"部署"),
        ("清空", r"清空"),
        ("格式化", r"格式化"),
        ("強制推送", r"強制推送|强制推送"),
    ]
]


def find_danger(text: str, extra_words: tuple[str, ...] | list[str] = ()) -> list[str]:
    """回傳 text 裡出現的危險字眼（不重複，依清單順序）；extra_words 是你在 .env 自訂的字眼。"""
    found = [label for label, pattern in DANGER_PATTERNS if pattern.search(text or "")]
    for word in extra_words:
        if word and word.lower() in (text or "").lower() and word not in found:
            found.append(word)
    return found


def _keep_head(value: str, keep: int = 4) -> str:
    """只保留開頭幾個字，其餘遮蔽，讓你還認得出是哪一把 key。"""
    return value[:keep] + MASK


# 各種常見的金鑰格式。遮蔽時保留開頭 4 個字。
_TOKEN_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),                      # Anthropic
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),                 # OpenAI
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),   # GitHub
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),                    # Slack
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),                        # AWS
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),                          # Google
    re.compile(r"(?<!\d)\d{8,10}:[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])"),   # Telegram bot token
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),   # JWT
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),                        # GitLab
    re.compile(r"hf_[A-Za-z0-9]{30,}"),                              # Hugging Face
]
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.DOTALL)
_BEARER_RE = re.compile(r"(Bearer\s+)([A-Za-z0-9._\-~+/]{16,}=*)", re.IGNORECASE)
# .env 形式：KEY=值、export KEY=值，鍵名含有 KEY/TOKEN/SECRET/PASSWORD 等
_ENV_ASSIGN_RE = re.compile(
    r"(?im)^(\s*(?:export\s+)?[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|PRIVATE)[A-Z0-9_]*\s*[=:]\s*)"
    r"([\"']?)([^\s\"']{4,})")
# JSON／YAML 形式："api_key": "值"
_KV_RE = re.compile(
    r"(?i)([\"']?[\w\-]*(?:api[_\-]?key|access[_\-]?key|token|secret|password|passwd)[\w\-]*[\"']?\s*[:=]\s*[\"'])"
    r"([^\"'\s]{6,})")


def mask_secrets(text: str) -> str:
    """把 text 裡疑似機密的字串遮蔽後回傳。"""
    if not text:
        return text
    text = _PRIVATE_KEY_RE.sub("[已遮蔽：私鑰內容]", text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(lambda m: _keep_head(m.group(0)), text)
    text = _BEARER_RE.sub(lambda m: m.group(1) + _keep_head(m.group(2)), text)
    text = _ENV_ASSIGN_RE.sub(
        lambda m: m.group(0) if MASK in m.group(3) else m.group(1) + m.group(2) + _keep_head(m.group(3), 2), text)
    text = _KV_RE.sub(lambda m: m.group(0) if MASK in m.group(2) else m.group(1) + _keep_head(m.group(2), 2), text)
    return text
