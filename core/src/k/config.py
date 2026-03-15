from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="K_")

    fs_base: Path
    media_policy_config_path: Path | None = None
    basic_os_user: str = "k"
    basic_os_addr: str = "k-container"
    basic_os_port: int = 22
    basic_os_sshkey: Path = Path(".ssh/id_ed25519")
