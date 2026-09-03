from ai_quota.redact import redact

_FAKE_GH_TOKEN = "gho_" + "A" * 36
_FAKE_OAI_KEY = "sk-" + "A" * 30


def test_redact_bearer_header():
    assert "abc123" not in redact("Authorization: Bearer abc123xyz")


def test_redact_gh_token_mid_string():
    s = f"url=https://x?token={_FAKE_GH_TOKEN}"
    out = redact(s)
    assert "gho_" not in out or "REDACTED" in out


def test_redact_openai_key():
    s = f"OPENAI_API_KEY={_FAKE_OAI_KEY}"
    out = redact(s)
    assert _FAKE_OAI_KEY[:6] not in out


def test_redact_cookie_header():
    assert "sess=abcdef" not in redact("Cookie: sess=abcdef; other=1")


def test_redact_apikey_case_insensitive():
    assert "secret123456789012345" not in redact("api_key: secret123456789012345")


def test_redact_passthrough_plain_text():
    assert redact("no secrets here") == "no secrets here"
