import pytest
from pydantic import ValidationError

from kapy.settings import Settings, load_settings


def test_explicit_aliases_no_automatic_dotenv_and_secret_redaction(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_MODEL=dotenv-model\nOPENAI_API_KEY=dotenv-secret\n")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert load_settings().model == "gpt-5.6-luna"
    settings = load_settings(env_file=str(tmp_path / ".env"))
    assert settings.model == "dotenv-model"
    assert "dotenv-secret" not in repr(settings)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("KAPY_CONTROL_TOKEN", "")
    assert load_settings().telegram_chat_id is None
    assert load_settings().control_token is None


def test_schema_and_machine_configuration_cannot_inject_identifiers():
    with pytest.raises(ValidationError):
        Settings(database_schema="public; DROP TABLE anything")
    with pytest.raises(ValidationError):
        Settings(machine_tokens={"contains.dot": "secret"})


def test_neutral_model_aliases_take_precedence_and_frontends_are_explicit(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "old-key")
    monkeypatch.setenv("OPENAI_MODEL", "old-model")
    old = Settings()
    assert old.model_api_key is not None
    assert old.model == "old-model" and old.model_api_key.get_secret_value() == "old-key"
    monkeypatch.setenv("KAPY_MODEL_BASE_URL", "https://new.invalid/compatible")
    monkeypatch.setenv("KAPY_MODEL_API_KEY", "new-key")
    monkeypatch.setenv("KAPY_MODEL", "vendor/model")
    monkeypatch.setenv("KAPY_FRONTENDS", "[]")
    settings = Settings(telegram_bot_token="leftover", telegram_chat_id=None)
    assert settings.model_base_url == "https://new.invalid/compatible"
    assert settings.model_api_key is not None
    assert settings.model_api_key.get_secret_value() == "new-key"
    assert settings.model == "vendor/model" and settings.enabled_frontends() == ()
