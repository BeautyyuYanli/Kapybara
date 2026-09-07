from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from kapy.execution import DaemonConfig


def test_config_is_pure_private_and_preserves_approved_fields(tmp_path: Path) -> None:
    config = DaemonConfig(
        machine_id="machine",
        gateway_url="ws://127.0.0.1:8765/rpc/machines/machine",
        machine_token=SecretStr("private-value"),
        cgroup_root=tmp_path / "not-created",
        child_env={"TEST_SECRET": "child-value"},
    )
    assert "private-value" not in repr(config)
    assert "child-value" not in repr(config)
    assert config.cgroup_root == tmp_path / "not-created"
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValidationError):
        config.__setattr__("machine_id", "other")


@pytest.mark.parametrize(
    "change",
    [
        {"gateway_url": "ws://example.com/rpc"},
        {"gateway_url": "wss://u:p@example.com/rpc"},
        {"machine_token": "bad\nheader"},
        {"machine_id": ""},
        {"idle_disconnect_after_s": 0},
        {"idle_reconnect_after_s": float("inf")},
        {"cgroup_root": "relative"},
        {"runtime_dir": "relative"},
        {"child_env": {"KAPY_SESSION_TOKEN": "override"}},
        {"unknown": 1},
    ],
)
def test_invalid_config_is_rejected(change: dict) -> None:
    with pytest.raises(ValidationError):
        DaemonConfig.model_validate(
            {
                "machine_id": "machine",
                "gateway_url": "wss://example.com/rpc/machines/machine",
                "machine_token": "token",
                **change,
            }
        )
