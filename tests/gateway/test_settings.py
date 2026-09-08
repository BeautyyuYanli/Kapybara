import pytest
from pydantic import ValidationError

from kapy.settings import Settings, load_settings


def test_dotenv_is_loaded_only_when_explicitly_selected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("KAPY_CONTROL_TOKEN=explicit\n")
    monkeypatch.delenv("KAPY_CONTROL_TOKEN", raising=False)
    assert load_settings().control_token is None
    settings = load_settings(env_file=str(tmp_path / ".env"))
    assert settings.control_token is not None
    assert settings.control_token.get_secret_value() == "explicit"
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    assert load_settings().telegram_chat_id is None


def test_schema_and_machine_configuration_cannot_inject_identifiers():
    with pytest.raises(ValidationError):
        Settings(database_schema="public; DROP TABLE anything")
    with pytest.raises(ValidationError):
        Settings(machine_tokens={"contains.dot": "secret"})


def test_control_starts_with_explicit_infrastructure_settings():
    settings = Settings(control_token="admin", session_signing_key="signing", frontends=[])
    settings.require_control()
    assert settings.enabled_frontends() == ()
