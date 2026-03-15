"""Application settings loaded from env/init plus TOML-backed multimodal policy.

`Config` is the single settings surface used across starters, runtime wiring,
and helper utilities. Historical fields such as `fs_base` and `basic_os_*`
keep their original `BaseSettings` source order: init kwargs first, then
`K_...` environment variables. The shared `~/.kapybara/config.toml` file is
only consulted for the nested `[multimodal]` section so multimodal policy can
be configured without changing legacy settings behavior.
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


class MultimodalTomlConfigSettingsSource(TomlConfigSettingsSource):
    """Expose only the `[multimodal]` TOML section as a settings source.

    Restricting the TOML source to one nested field preserves the legacy source
    ordering for all existing top-level settings while still allowing
    multimodal presets and custom policies to live in the shared user config.
    """

    def __call__(self) -> dict[str, object]:
        data = super().__call__()
        multimodal = data.get("multimodal")
        if multimodal is None:
            return {}
        return {"multimodal": multimodal}


class Config(BaseSettings):
    """Application settings with optional TOML-backed multimodal policy.

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
        """Inject `[multimodal]` TOML data without changing legacy field priority."""

        toml_settings = MultimodalTomlConfigSettingsSource(
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
