"""Best-effort secret stripping at the capture boundary.

Runs over every `Event.content_excerpt` before anything leaves the machine.
This is a feature we will advertise, but it is best-effort and not a
guarantee — see DESIGN.md §9.

What we strip (and replace with a typed placeholder like `[REDACTED:openai_key]`):
    - OpenAI keys (sk-..., sk-proj-...)
    - Anthropic keys (sk-ant-...)
    - GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_, github_pat_)
    - Slack tokens (xox[abprs]-...)
    - AWS access key IDs (AKIA / ASIA + 16 chars)
    - Google API keys (AIza...)
    - Bearer tokens in Authorization headers (keeps "Bearer" visible)
    - URLs with embedded credentials (https://user:pass@host — keeps scheme/host)
    - JWTs (three base64url segments separated by dots)
    - Generic high-entropy hex/base64 strings >= 40 chars (last-resort heuristic)

Patterns are ordered most-specific to least-specific so the typed labels are
preferred over the generic catch-all.
"""

from __future__ import annotations

import re
from typing import Final

# Each entry: (compiled regex, replacement string). Order matters — specific first.
_PATTERNS: Final[list[tuple[re.Pattern[str], str]]] = [
    # Anthropic must come before generic openai sk- to take precedence on sk-ant-.
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED:anthropic_key]"),
    (re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), "[REDACTED:openai_key]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED:github_token]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED:github_token]"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack_token]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:aws_key_id]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35,}\b"), "[REDACTED:google_api_key]"),
    # Bearer tokens — keep "Bearer " visible so the line still reads as auth.
    (
        re.compile(r"(?i)(Authorization:\s*Bearer\s+)[A-Za-z0-9._~+/=\-]{12,}"),
        r"\1[REDACTED:bearer_token]",
    ),
    # URLs with embedded credentials — keep scheme and host.
    (
        re.compile(r"\b([a-z][a-z0-9+\-.]*://)[^/\s:@]+:[^/\s@]+@"),
        r"\1[REDACTED:url_credentials]@",
    ),
    # JWTs.
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
        "[REDACTED:jwt]",
    ),
    # Last-resort heuristics. Tuned conservatively to avoid mangling normal prose.
    (re.compile(r"\b[A-Fa-f0-9]{40,}\b"), "[REDACTED:high_entropy_hex]"),
    (re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}\b"), "[REDACTED:high_entropy_b64]"),
]


def sanitize(text: str) -> str:
    """Strip likely secrets from a content excerpt.

    Returns the redacted text. Each match is replaced with `[REDACTED:<label>]`
    so downstream consumers can tell what was removed without seeing it.
    Idempotent: running twice produces the same result.
    """
    if not text:
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out
