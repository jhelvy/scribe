"""Secret scrubbing.

Capturing full tool output means `cat .env`, `env`, and a curl with an auth
header all land on disk. The logs live in a 0700 directory outside every repo,
but defence in depth is cheap here: run every rendered string through a pattern
set before it reaches a file or a browser.

This is a safety net, not a guarantee. It catches the shapes that leak most
often — provider key prefixes, `Authorization` headers, and `KEY=value`
assignments — and says so plainly rather than implying completeness.
"""

from __future__ import annotations

import re

MASK = "[redacted]"

# Ordered: the specific, high-confidence prefixes first, then the generic
# assignment shape which is the one that can false-positive.
BUILTIN_PATTERNS: list[tuple[str, str]] = [
    # Provider / platform keys with distinctive prefixes
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "anthropic-key"),
    (r"sk-[A-Za-z0-9]{32,}", "openai-key"),
    (r"gh[pousr]_[A-Za-z0-9]{16,}", "github-token"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "github-pat"),
    (r"xox[abposr]-[A-Za-z0-9-]{10,}", "slack-token"),
    (r"AKIA[0-9A-Z]{16}", "aws-access-key"),
    (r"AIza[0-9A-Za-z_\-]{35}", "google-api-key"),
    (r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}", "jwt"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", "private-key"),
    # Headers
    (r"(?i)(authorization\s*:\s*(?:bearer|basic|token)\s+)[^\s\"']{8,}", "auth-header"),
    (r"(?i)(x-api-key\s*:\s*)[^\s\"']{8,}", "api-key-header"),
    # Generic assignments: KEY=..., "secret": "...", DATABASE_PASSWORD="...".
    # The name may carry a prefix or suffix (`DATABASE_PASSWORD`, `api_key_2`),
    # so a plain \b before the keyword is not enough — underscore is a word
    # character, so \bpassword never matches inside DATABASE_PASSWORD.
    (
        r"(?i)(?<![A-Za-z0-9_.\-])([A-Za-z0-9_.\-]{0,32}?(?:api[_\-]?key"
        r"|secret[_\-]?key|access[_\-]?token|auth[_\-]?token|client[_\-]?secret"
        r"|passwd|password|secret|token)[A-Za-z0-9_.\-]{0,32}?\s*[=:]\s*[\"']?)"
        r"([^\s\"',;}]{6,})",
        "assignment",
    ),
]

# Every rule above needs one of these substrings to have any chance of matching.
# Scanning for them once is far cheaper than running thirteen patterns over
# every string, and the overwhelming majority of a transcript — prose, code,
# file paths, command output — contains none of them. Without this, redaction
# was two thirds of the cost of rendering a log.
_TRIGGER = re.compile(
    r"(?i)sk-|gh[pousr]_|github_pat_|xox|AKIA|AIza|eyJ|BEGIN [A-Z ]*PRIVATE"
    r"|authoriz|x-api-key|key|secret|token|passw"
)

# Values that match the generic assignment shape but are obviously not secrets.
_INNOCUOUS = re.compile(
    r"^(?:true|false|null|none|undefined|yes|no|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
    r"|<[^>]*>|\[[^\]]*\]|\.\.\.|x{3,}|\*{3,}|\.{3,}"
    r"|your[_\-].*|my[_\-].*|some[_\-].*|example.*|placeholder.*|changeme.*"
    r"|" + re.escape(MASK) + r")$",
    re.I,
)


class Redactor:
    def __init__(self, enabled: bool = True, extra_patterns: list[str] | None = None):
        self.enabled = enabled
        self.rules: list[tuple[re.Pattern, str]] = []
        # User patterns can match anything, so the trigger pre-filter is only
        # safe to apply when there are none.
        self.custom = False
        if not enabled:
            return
        for pattern, name in BUILTIN_PATTERNS:
            try:
                self.rules.append((re.compile(pattern), name))
            except re.error:
                continue
        for pattern in extra_patterns or []:
            try:
                self.rules.append((re.compile(pattern), "custom"))
                self.custom = True
            except re.error:
                continue  # a user typo in config must not break logging

    def __call__(self, text: str) -> str:
        return self.scrub(text)

    def scrub(self, text):
        if not self.enabled or not text or not isinstance(text, str):
            return text
        if not self.custom and not _TRIGGER.search(text):
            return text  # nothing here can match; skip thirteen scans
        out = text
        for pattern, name in self.rules:
            if name == "assignment":
                out = pattern.sub(_mask_assignment, out)
            elif name in ("auth-header", "api-key-header"):
                out = pattern.sub(lambda m: m.group(1) + MASK, out)
            else:
                out = pattern.sub(MASK, out)
        return out

    def scrub_data(self, value):
        """Recursively scrub a JSON-ish structure (tool inputs, mostly)."""
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, list):
            return [self.scrub_data(v) for v in value]
        if isinstance(value, dict):
            return {k: self.scrub_data(v) for k, v in value.items()}
        return value


def _mask_assignment(match: re.Match) -> str:
    prefix, value = match.group(1), match.group(2)
    if _INNOCUOUS.match(value):
        return match.group(0)
    return prefix + MASK


def from_config(cfg: dict) -> Redactor:
    section = (cfg or {}).get("redact") or {}
    return Redactor(
        enabled=bool(section.get("enabled", True)),
        extra_patterns=section.get("extra_patterns") or [],
    )
