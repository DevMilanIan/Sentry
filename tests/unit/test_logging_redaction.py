from app.observability.logging import redact_sensitive


def test_redacts_nested_credentials_without_removing_useful_context() -> None:
    event = {
        "event": "connection failed",
        "request": {"Authorization": "private", "items": [{"refresh_token": "private"}]},
        "error": "postgresql://user:private@localhost/db Bearer abc.secret",
        "account_fingerprint": "private",
        "count": 3,
    }
    result = redact_sensitive(None, "warning", event)
    assert result["request"] == {
        "Authorization": "[REDACTED]",
        "items": [{"refresh_token": "[REDACTED]"}],
    }
    assert "private" not in str(result)
    assert "abc.secret" not in str(result)
    assert result["event"] == "connection failed"
    assert result["count"] == 3
