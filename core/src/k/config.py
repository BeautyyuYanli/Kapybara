"""Application settings loaded from TOML, environment, and init kwargs.

`Config` is the single settings surface used across starters, runtime wiring,
and helper utilities. Nested models are used for structured sections so
`~/.kapybara/config.toml`, `K_...` environment overrides, and direct
instantiation all share one schema.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from k.multimodal_config import (
    DEFAULT_MEDIA_POLICY_NAME,
    MultimodalConfig,
    MultimodalConversionRuleConfig,
    MultimodalCustomPolicyConfig,
    resolve_multimodal_policy_config,
)

__all__ = [
    "DEFAULT_MEDIA_POLICY_NAME",
    "Config",
    "MultimodalConfig",
    "MultimodalConversionRuleConfig",
    "MultimodalCustomPolicyConfig",
    "default_user_config_toml_path",
    "resolve_multimodal_policy_config",
]


def default_user_config_toml_path() -> Path:
    """Return the per-user config path, resolving `~` at instantiation time.

    The path is expanded lazily so tests and wrappers that override `HOME`
    before constructing `Config` pick up the expected user config file.
    """

    return Path("~/.kapybara/config.toml").expanduser()


class Config(BaseSettings):
    """Application settings with `~/.kapybara/config.toml` as the highest priority.

    Unknown TOML sections are ignored so the shared user config file can store
    settings for other entrypoints without breaking `Config`.
    """

    model_config = SettingsConfigDict(
        env_prefix="K_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    fs_base: Path
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)
    basic_os_user: str = "k"
    basic_os_addr: str = "k-container"
    basic_os_port: int = 22
    basic_os_sshkey: Path = Path(".ssh/id_ed25519")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Load the user TOML file before init kwargs or environment overrides."""

        toml_settings = TomlConfigSettingsSource(
            settings_cls,
            toml_file=default_user_config_toml_path(),
        )
        return (
            toml_settings,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
