import pytest
from pydantic import ValidationError

from kapy.settings import Settings, load_settings


def test_no_automatic_dotenv_and_removed_server_model_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=dotenv-secret\nKAPY_CONTROL_TOKEN=explicit\n")
    monkeypatch.delenv("KAPY_CONTROL_TOKEN", raising=False)
    assert load_settings().control_token is None
    settings = load_settings(env_file=str(tmp_path / ".env"))
    assert settings.control_token is not None
    assert settings.control_token.get_secret_value() == "explicit"
    assert "dotenv-secret" not in repr(settings)
    assert "model_api_key" not in Settings.model_fields
    assert "context_window_tokens" not in Settings.model_fields
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    assert load_settings().telegram_chat_id is None


def test_schema_and_machine_configuration_cannot_inject_identifiers():
    with pytest.raises(ValidationError):
        Settings(database_schema="public; DROP TABLE anything")
    with pytest.raises(ValidationError):
        Settings(machine_tokens={"contains.dot": "secret"})


def test_control_starts_without_model_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ignored-secret")
    settings = Settings(control_token="admin", session_signing_key="signing", frontends=[])
    settings.require_control()
    assert settings.enabled_frontends() == ()
    assert "ignored-secret" not in repr(settings)
