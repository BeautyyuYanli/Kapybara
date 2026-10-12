"""The CLI issues one opaque credential and keeps input secrets out of errors."""

import json
import os
import subprocess
import sys
from pathlib import Path

from mcp_gateway import TokenCodec, decode_key


def test_cli_keygen_issue_and_redacted_errors(tmp_path: Path):
    command = [sys.executable, "-m", "mcp_gateway"]
    key = subprocess.run(
        [*command, "keygen"], capture_output=True, text=True, check=True
    ).stdout.strip()
    config = tmp_path / "gateway.json"
    config.write_text(
        json.dumps(
            {
                "servers": {
                    "api": {
                        "transport": "http",
                        "url": "http://127.0.0.1:9000/mcp",
                    }
                }
            }
        )
    )
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"api": {"bearer": "input-secret"}}))
    env = os.environ | {"MCP_GATEWAY_JWE_KEY": key}
    issue = [
        *command,
        "issue",
        "--config",
        str(config),
        "--credentials",
        str(credentials),
        "--subject",
        "alice",
    ]
    result = subprocess.run(issue, env=env, capture_output=True, text=True, check=True)
    codec = TokenCodec({"primary": decode_key(key)}, active_key_id="primary")
    token = result.stdout.strip()
    assert len(result.stdout.splitlines()) == 1 and result.stderr == ""
    bearer = codec.decode(token).upstreams["api"].bearer
    assert bearer is not None and bearer.get_secret_value() == "input-secret"

    credentials.write_text(json.dumps({"api": {"bearer": "input-secret", "url": "http://secret"}}))
    result = subprocess.run(issue, env=env, capture_output=True, text=True)
    assert result.returncode == 2 and result.stdout == ""
    assert "input-secret" not in result.stderr and "http://secret" not in result.stderr
    result = subprocess.run(
        issue, env=env | {"MCP_GATEWAY_JWE_KEY": "invalid-secret"}, capture_output=True, text=True
    )
    assert result.returncode == 2 and "invalid-secret" not in result.stderr
