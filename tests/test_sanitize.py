"""Tests for the secret-stripping sanitizer."""

from __future__ import annotations

import pytest

from billable.core.sanitize import sanitize


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("my key is sk-abc1234567890ABCDEF12345 yo", "openai_key"),
        ("OPENAI=sk-proj-Abc123_-XYZdefGHIjkl0123456 here", "openai_key"),
        ("anthropic sk-ant-api03-abcDEFghi12345JKLmno678", "anthropic_key"),
        ("token=ghp_AbCdEfGhIjKlMnOpQrStUv1234567890", "github_token"),
        (
            "token=github_pat_11ABCDEFG0abcdefghijklMnopqrstuVWXYZ_0123456789abc",
            "github_token",
        ),
        ("xoxb-12345-67890-abcdEFGHijkl", "slack_token"),
        ("AWS=AKIAIOSFODNN7EXAMPLE done", "aws_key_id"),
        ("key: AIzaSyA-abcDEF1234567890_GHIjklMNOpqrSTUvw01", "google_api_key"),
    ],
)
def test_known_secrets_are_redacted(raw: str, label: str) -> None:
    out = sanitize(raw)
    assert f"[REDACTED:{label}]" in out
    # The original secret material should be gone.
    assert "sk-abc1234567890ABCDEF12345" not in out
    assert "ghp_AbCdEfGhIjKlMnOpQrStUv1234567890" not in out


def test_bearer_token_keeps_prefix() -> None:
    line = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
    out = sanitize(line)
    assert "Authorization: Bearer [REDACTED:" in out
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out


def test_url_credentials_keeps_scheme_and_host() -> None:
    line = "Cloning from https://alice:supersecret123@github.com/acme/x"
    out = sanitize(line)
    assert "https://[REDACTED:url_credentials]@github.com/acme/x" in out
    assert "alice:supersecret123" not in out


def test_jwt_redacted() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    out = sanitize(f"token={jwt} done")
    assert "[REDACTED:jwt]" in out
    assert jwt not in out


def test_normal_prose_passes_through() -> None:
    text = (
        "Drafted architecture plan for activity-tracking agent, "
        "covering capture sources and the two-stage LLM pipeline."
    )
    assert sanitize(text) == text


def test_short_hex_left_alone() -> None:
    # A 12-char hex string is below the 40-char threshold; should not be touched.
    assert sanitize("commit abc123def456") == "commit abc123def456"


def test_long_hex_redacted() -> None:
    # 40-char hex string → high-entropy heuristic.
    sha = "a" * 40
    out = sanitize(f"sha={sha} done")
    assert "[REDACTED:high_entropy_hex]" in out


def test_idempotent() -> None:
    line = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
    once = sanitize(line)
    twice = sanitize(once)
    assert once == twice


def test_empty_string_passthrough() -> None:
    assert sanitize("") == ""
