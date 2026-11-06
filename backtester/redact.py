"""Display redaction, on by default: mask secret-shaped strings and shorten paths."""

from __future__ import annotations

import re

MASK = "[REDACTED]"

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{12,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_.-]*(?:api[_-]?key|token|secret|passw(?:or)?d|credential)[A-Z0-9_.-]*\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s&;|]+)"
)
_HOME_PATH = re.compile(r"(?:/Users|/home)/[^/\s\"']+")
_LONG_PATH = re.compile(r"(?<![\w.~])(/(?:[^/\s\"']+/){3,})([^/\s\"']+)")


def mask_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(MASK, text)
    return _SECRET_ASSIGNMENT.sub(lambda m: m.group(1) + MASK, text)


def shorten_paths(text: str) -> str:
    text = _HOME_PATH.sub("~", text)
    return _LONG_PATH.sub(lambda m: ".../" + m.group(1).rstrip("/").rsplit("/", 1)[-1] + "/" + m.group(2), text)


def redact(text: str) -> str:
    return shorten_paths(mask_secrets(text))
