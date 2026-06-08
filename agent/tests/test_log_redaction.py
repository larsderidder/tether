import pytest

from tether.log_redaction import make_log_redactor


@pytest.mark.anyio
async def test_redacts_authorization_header() -> None:
    redactor = make_log_redactor()
    event_dict = {
        "headers": {"Authorization": "Bearer supersecret"},
        "event": "test",
    }
    out = redactor(None, "test", event_dict)
    # payload-redactor redacts by keyword match; ensure secret doesn't leak.
    assert "supersecret" not in str(out)


@pytest.mark.anyio
async def test_redacts_bearer_string_anywhere() -> None:
    redactor = make_log_redactor()
    event_dict = {"event": "error", "body": "oops Authorization: Bearer abc.def.ghi"}
    out = redactor(None, "test", event_dict)
    assert "abc.def.ghi" not in str(out)


@pytest.mark.anyio
async def test_redacts_telegram_token_in_exception_text() -> None:
    redactor = make_log_redactor()
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    event_dict = {"event": "error", "exception": f"ExtBot[token={token}]"}

    out = redactor(None, "test", event_dict)

    assert token not in str(out)
    assert "[REDACTED]" in str(out)
