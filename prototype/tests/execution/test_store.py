import asyncio
import os
from pathlib import Path

import pytest

from kapy.execution import resolve_paths
from kapy.execution.store import ExecutionStore
from kapy.rpc import RpcError


def paths_at(root: Path):
    return resolve_paths(state_dir=root / "state", data_dir=root / "data", runtime_dir=root / "run")


@pytest.mark.asyncio
async def test_sessions_persist_isolate_and_do_not_persist_tokens(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    token = "a-credential-only-kept-in-memory"
    async with ExecutionStore(paths, "machine") as store:
        results = await asyncio.gather(
            *(store.ensure_session(f"session-{i}", token) for i in range(32))
        )
        assert len({item["cwd"] for item in results}) == 32
        assert store.authenticate_session("session-0", token)
        assert not store.authenticate_session("session-0", "wrong")
        original = await store.session_cwd("session-0", require_token=True)
        assert (await store.ensure_session("session-0", "replacement"))["cwd"] == str(original)
        assert not store.authenticate_session("session-0", token)
    async with ExecutionStore(paths, "machine") as store:
        assert await store.session_cwd("session-0") == original
        with pytest.raises(RpcError) as caught:
            await store.session_cwd("session-0", require_token=True)
        assert caught.value.code == -32001
        await store.ensure_session("session-0", "new")
    for path in paths.state_dir.iterdir():
        if path.is_file():
            assert token.encode() not in await asyncio.to_thread(path.read_bytes)
            assert path.stat().st_mode & 0o077 == 0
    assert os.getcwd() != str(original)


@pytest.mark.asyncio
async def test_state_and_runtime_both_exclude_another_daemon(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    async with ExecutionStore(paths, "machine"):
        with pytest.raises(RuntimeError, match="Another daemon"):
            async with ExecutionStore(paths, "machine"):
                pytest.fail("The state lock allowed another daemon")
        other = resolve_paths(
            state_dir=tmp_path / "other-state",
            data_dir=tmp_path / "other-data",
            runtime_dir=paths.runtime_dir,
        )
        with pytest.raises(RuntimeError, match="Another daemon"):
            async with ExecutionStore(other, "machine"):
                pytest.fail("The runtime lock allowed another daemon")
    async with ExecutionStore(paths, "machine"):
        pass
    with pytest.raises(RuntimeError, match="different machine_id"):
        async with ExecutionStore(paths, "another-machine"):
            pytest.fail("The database machine binding was ignored")


@pytest.mark.asyncio
async def test_release_is_durable_and_never_reuses_identity(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    async with ExecutionStore(paths, "machine") as store:
        await store.ensure_session("session", "token")
        cwd = await store.session_cwd("session")
        await asyncio.to_thread((cwd / "owned").write_text, "owned data")
        assert await store.begin_release("session")
        assert not store.authenticate_session("session", "token")
        with pytest.raises(RpcError) as caught:
            await store.ensure_session("session", "token")
        assert caught.value.code == -32009
    async with ExecutionStore(paths, "machine") as store:
        assert await store.begin_release("session")
        await store.finish_release("session")
        assert not cwd.exists()
        assert not await store.begin_release("session")
        with pytest.raises(RpcError) as caught:
            await store.ensure_session("session", "fresh")
        assert caught.value.code == -32010


@pytest.mark.asyncio
async def test_private_directories_are_checked_not_silently_chmodded(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    paths.runtime_dir.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match="0700"):
        async with ExecutionStore(paths, "machine"):
            pytest.fail("Unsafe runtime permissions accepted")
    assert paths.runtime_dir.stat().st_mode & 0o777 == 0o755
