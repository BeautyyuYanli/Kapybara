import pytest
from pydantic import ValidationError

from kapy.settings import Settings, load_settings


def test_explicit_aliases_no_automatic_dotenv_and_secret_redaction(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_MODEL=dotenv-model\nOPENAI_API_KEY=dotenv-secret\n")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert load_settings().openai_model == "gpt-5.6-luna"
    settings = load_settings(env_file=str(tmp_path / ".env"))
    assert settings.openai_model == "dotenv-model"
    assert "dotenv-secret" not in repr(settings)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("KAPY_CONTROL_TOKEN", "")
    assert load_settings().telegram_chat_id is None
    assert load_settings().control_token is None
    assert "cgroup_root" not in Settings.model_fields


def test_schema_and_machine_configuration_cannot_inject_identifiers():
    with pytest.raises(ValidationError):
        Settings(database_schema="public; DROP TABLE anything")
    with pytest.raises(ValidationError):
        Settings(machine_tokens={"contains.dot": "secret"})
