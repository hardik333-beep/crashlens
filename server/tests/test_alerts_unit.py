"""Unit tests for the alerts slice -- no Postgres, Redis, or app.

Covers the pure formatting helpers (subject / link / body / webhook payload),
channel-config validation, URL masking, and the SMTP sender itself.

SMTP TESTING NOTE: ``aiosmtpd`` is NOT installed and is deliberately NOT added as
a dependency, so the email send path is proven by MONKEYPATCHING
``smtplib.SMTP`` with a recording fake and asserting the composed message
(headers + body) and the STARTTLS/auth calls. If ``aiosmtpd`` were available a
real in-process SMTP sink would be preferable; this file flags that it used the
monkeypatch path instead.
"""

import pytest

from app import alerts
from app.config import Settings
from app.jobs import alerts as dispatch


# --- Message formatting -------------------------------------------------------
def test_alert_subject_new_vs_regression() -> None:
    assert dispatch.alert_subject("new", "Payments", "KeyError: id") == (
        "[Crashlens] New error in Payments: KeyError: id"
    )
    assert dispatch.alert_subject("regression", "Payments", "KeyError: id") == (
        "[Crashlens] Error came back in Payments: KeyError: id"
    )


def test_alert_link_relative_without_base_url() -> None:
    link = dispatch.alert_link(None, "org1", "proj1", "issue1")
    assert link == "/org/org1/projects/proj1/issues/issue1"


def test_alert_link_absolute_with_base_url_trims_trailing_slash() -> None:
    link = dispatch.alert_link("https://crashlens.example.com/", "o", "p", "i")
    assert link == "https://crashlens.example.com/org/o/projects/p/issues/i"


def test_alert_body_contains_title_level_and_link() -> None:
    body = dispatch.alert_body("new", "Payments", "KeyError: id", "error", "/x")
    assert "Payments" in body
    assert "KeyError: id" in body
    assert "error" in body
    assert "/x" in body
    assert "new error" in body.lower()


def test_alert_body_regression_wording() -> None:
    body = dispatch.alert_body("regression", "Payments", "T", "error", "/x")
    assert "again" in body.lower()


def test_webhook_payload_shape() -> None:
    payload = dispatch.webhook_payload(
        "new", "proj1", "issue1", "T", "error", "2026-07-04T00:00:00+00:00"
    )
    assert payload == {
        "kind": "new",
        "project_id": "proj1",
        "issue_id": "issue1",
        "title": "T",
        "level": "error",
        "ts": "2026-07-04T00:00:00+00:00",
    }


# --- Config validation --------------------------------------------------------
def test_validate_email_config_defaults_to_empty() -> None:
    assert alerts.validate_channel_config("email", None) == {}
    assert alerts.validate_channel_config("email", {}) == {}
    assert alerts.validate_channel_config("email", {"to": []}) == {}


def test_validate_email_config_keeps_valid_recipients() -> None:
    config = alerts.validate_channel_config(
        "email", {"to": ["a@example.com", " b@example.com "]}
    )
    assert config == {"to": ["a@example.com", "b@example.com"]}


def test_validate_email_config_rejects_bad_address() -> None:
    with pytest.raises(alerts.ChannelConfigError):
        alerts.validate_channel_config("email", {"to": ["not-an-email"]})


def test_validate_email_config_rejects_non_list_to() -> None:
    with pytest.raises(alerts.ChannelConfigError):
        alerts.validate_channel_config("email", {"to": "a@example.com"})


def test_validate_slack_requires_https_webhook_url() -> None:
    config = alerts.validate_channel_config(
        "slack", {"webhook_url": "https://hooks.slack.com/services/T/B/x"}
    )
    assert config == {"webhook_url": "https://hooks.slack.com/services/T/B/x"}


def test_validate_slack_rejects_missing_or_http_url() -> None:
    with pytest.raises(alerts.ChannelConfigError):
        alerts.validate_channel_config("slack", {})
    with pytest.raises(alerts.ChannelConfigError):
        alerts.validate_channel_config("slack", {"webhook_url": "http://insecure/x"})


def test_validate_webhook_requires_https_url() -> None:
    config = alerts.validate_channel_config("webhook", {"url": "https://ex.com/hook"})
    assert config == {"url": "https://ex.com/hook"}


def test_validate_webhook_strips_unknown_keys() -> None:
    config = alerts.validate_channel_config(
        "webhook", {"url": "https://ex.com/hook", "secret": "leak"}
    )
    assert config == {"url": "https://ex.com/hook"}


def test_validate_rejects_unknown_type() -> None:
    with pytest.raises(alerts.ChannelConfigError):
        alerts.validate_channel_config("carrier-pigeon", {})


# --- URL / target masking -----------------------------------------------------
def test_mask_target_email_all_members_default() -> None:
    assert alerts.mask_target("email", {}) == "All team members"


def test_mask_target_email_explicit_recipients() -> None:
    assert alerts.mask_target("email", {"to": ["a@x.com", "b@x.com"]}) == (
        "a@x.com, b@x.com"
    )


def test_mask_target_slack_hides_secret_path() -> None:
    masked = alerts.mask_target(
        "slack", {"webhook_url": "https://hooks.slack.com/services/T/B/SECRET"}
    )
    assert masked == "https://hooks.slack.com/..."
    assert "SECRET" not in masked


def test_mask_target_webhook_hides_query_token() -> None:
    masked = alerts.mask_target(
        "webhook", {"url": "https://ops.example.com/hook?token=SECRET"}
    )
    assert masked == "https://ops.example.com/..."
    assert "SECRET" not in masked


def test_mask_target_handles_missing_url() -> None:
    assert alerts.mask_target("webhook", {}) == "(not set)"


# --- smtp_is_configured -------------------------------------------------------
def _settings(**overrides: object) -> Settings:
    base = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "redis_url": "redis://localhost:6379/0",
        "secret_key": "x",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_smtp_is_configured_requires_host_and_from() -> None:
    assert dispatch.smtp_is_configured(_settings()) is False
    assert dispatch.smtp_is_configured(_settings(smtp_host="mail")) is False
    assert (
        dispatch.smtp_is_configured(_settings(smtp_host="mail", smtp_from="a@x.com"))
        is True
    )


# --- SMTP send (monkeypatched smtplib.SMTP; aiosmtpd not installed) -----------
class _FakeSMTP:
    """Records the composed message and the STARTTLS/login calls."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: int) -> None:  # noqa: ARG002
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None
        self.sent: object = None
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.logged_in = (username, password)

    def send_message(self, message: object) -> None:
        self.sent = message


def test_send_email_composes_message_and_uses_starttls_and_auth(monkeypatch) -> None:
    _FakeSMTP.instances = []
    monkeypatch.setattr(dispatch.smtplib, "SMTP", _FakeSMTP)
    settings = _settings(
        smtp_host="mail.example.com",
        smtp_from="alerts@example.com",
        smtp_username="user",
        smtp_password="pass",
        smtp_starttls=True,
    )

    dispatch.send_email(
        ["dev@example.com"], "[Crashlens] New error in P: T", "body text", settings
    )

    assert len(_FakeSMTP.instances) == 1
    server = _FakeSMTP.instances[0]
    assert server.host == "mail.example.com"
    assert server.started_tls is True
    assert server.logged_in == ("user", "pass")
    assert server.sent is not None
    assert server.sent["From"] == "alerts@example.com"
    assert server.sent["To"] == "dev@example.com"
    assert server.sent["Subject"] == "[Crashlens] New error in P: T"
    assert "body text" in server.sent.get_content()


def test_send_email_skips_auth_when_no_credentials(monkeypatch) -> None:
    _FakeSMTP.instances = []
    monkeypatch.setattr(dispatch.smtplib, "SMTP", _FakeSMTP)
    settings = _settings(
        smtp_host="mail.example.com",
        smtp_from="alerts@example.com",
        smtp_starttls=False,
    )

    dispatch.send_email(["dev@example.com"], "S", "B", settings)

    server = _FakeSMTP.instances[0]
    assert server.started_tls is False
    assert server.logged_in is None
