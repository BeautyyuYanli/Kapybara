"""Installed sync/aio APIs against caller-owned tmux 3.5+ and real terminal input."""

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import opensol as sdk
from opensol import aio


class TmuxFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="opensol-tmux-")
        self.addCleanup(self.directory.cleanup)
        self.socket = str(Path(self.directory.name) / "tmux.sock")
        subprocess.run(["tmux", "-S", self.socket, "-f", "/dev/null", "new-session", "-d",
                        "-s", "user-session", "sleep", "infinity"], check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["tmux", "-N", "-S", self.socket, "kill-server"],
                        capture_output=True)
        self.manager = sdk.tmux(sdk.local(), socket_path=self.socket)


class TmuxTests(TmuxFixture, unittest.TestCase):
    def test_local_binding_capabilities_validation_and_native_failure(self):
        missing_socket = str(Path(self.directory.name) / "never-started.sock")
        missing = sdk.tmux(sdk.local(), socket_path=missing_socket)
        capabilities = missing.capabilities()
        for name in ("list", "restart", "kill", "remove", "terminal_snapshot", "stdin",
                     "binary_stdin", "pty", "resize", "detached", "reconnect"):
            self.assertTrue(capabilities[name], name)
        for name in ("output_resource", "historical_output", "streaming_output",
                     "daemon_restart_recovery", "pause", "start", "close_stdin"):
            self.assertFalse(capabilities[name], name)
        self.assertEqual(capabilities["supported_signals"], [])
        bound = missing.get(sdk.ProcessRef("tmux", "opaque-unchecked"))
        self.assertEqual(bound.ref.id, "opaque-unchecked")
        with self.assertRaises(sdk.UnsupportedError):
            missing.spawn(["true"])
        with self.assertRaises(sdk.TransportError):
            missing.probe()
        self.assertFalse(Path(missing_socket).exists())
        for options in ({"socket_path": "relative"}, {"socket_path": "/tmp/x", "session_prefix": "x;y"},
                        {"socket_path": "/tmp/x", "session_prefix": ""},
                        {"socket_path": "/tmp/x", "program": ""}):
            with self.subTest(options=options), self.assertRaises(sdk.InvalidArgumentError):
                sdk.tmux(sdk.local(), **options)
        absent = sdk.tmux(sdk.local(), socket_path=self.socket, program="/no-private-tmux")
        with self.assertRaises(sdk.NotFoundError) as raised:
            absent.probe()
        self.assertEqual((raised.exception.backend, raised.exception.operation), ("tmux", "probe"))
        self.assertNotIn("no-private-tmux", str(raised.exception))
        info = self.manager.probe()
        self.assertEqual(info.backend, "tmux")
        self.assertTrue(info.client_version)
        self.assertTrue(info.server_version)
        self.assertEqual(self.manager.list(), [])

    def test_sync_binary_input_resize_screen_and_completion_only_subscription(self):
        payload = bytes(range(256)) * 32
        handle = self.manager.spawn(["python3", "-u", "-c",
            "import os,tty,hashlib; tty.setraw(0); print('READY',flush=True); data=b''\n"
            "while len(data)<8192: data+=os.read(0,8192-len(data))\n"
            "print(hashlib.sha256(data).hexdigest(), os.get_terminal_size(0),flush=True); raise SystemExit(7)"],
            stdin=True, pty=(101, 29))
        process = self.manager.get(handle.process)
        self.assertIs(handle.process.pty, True)
        for _ in range(100):
            if b"READY" in process.output.snapshot():
                break
            time.sleep(.02)
        else:
            self.fail("raw terminal did not become ready")
        for call in (process.output.stat, process.output.read, process.pause, process.start,
                     process.close_stdin, lambda: process.signal(2)):
            with self.assertRaises(sdk.UnsupportedError):
                call()
        with self.assertRaises(sdk.InvalidArgumentError):
            process.send_input(b"x" * 8193)
        process.send_input(b"")
        with self.assertRaises(sdk.TimeoutError):
            handle.next_event(timeout=.05)
        with self.assertRaises(sdk.ConflictError):
            process.remove()
        process.resize(107, 33)
        process.send_input(payload)
        event = handle.next_event(timeout=5)
        self.assertIsNone(event.channel)
        self.assertEqual(event.data, b"")
        self.assertEqual(event.outcome.exit.code, 7)
        self.assertIsNone(handle.next_event(timeout=1))
        self.assertEqual(handle.wait().exit.code, 7)
        output = process.output.snapshot()
        self.assertIsInstance(output, bytes)
        self.assertIn(hashlib.sha256(payload).hexdigest().encode(), output)
        self.assertIn(b"columns=107, lines=33", output)
        self.assertEqual(process.wait(timeout=5).exit.code, 7)
        self.assertEqual(self.manager.list()[0].process.id, handle.process.id)
        process.remove()
        with self.assertRaises(sdk.NotFoundError):
            process.output.snapshot()

    def test_restart_reconnection_execution_cache_and_external_respawn(self):
        marker = str(Path(self.directory.name) / "execution")
        handle = self.manager.spawn(["python3", "-c",
            "from pathlib import Path; import sys; p=Path(sys.argv[1]); old=p.exists(); p.touch(); "
            "print('new-screen' if old else 'old-screen'); sys.exit(9 if old else 7)", marker], pty=(80, 24))
        process = self.manager.get(handle.process)
        self.assertEqual(handle.wait().exit.code, 7)
        original = process.output.subscribe()
        process.restart()
        with self.assertRaises(sdk.ConflictError):
            original.wait()
        self.assertEqual(handle.wait().exit.code, 7)
        self.assertEqual(process.wait(timeout=5).exit.code, 9)
        self.assertIn(b"new-screen", process.output.snapshot())
        rebound = sdk.tmux(sdk.local(), socket_path=self.socket).get(
            sdk.ProcessRef("tmux", handle.process.id))
        self.assertEqual(rebound.output.subscribe().wait().exit.code, 9)
        old = rebound.output.subscribe()
        pane = json.loads(handle.process.id)["pane"]
        subprocess.run(["tmux", "-N", "-S", self.socket, "respawn-pane", "-t", pane,
                        "sleep", "30"], check=True, capture_output=True)
        with self.assertRaises(sdk.ConflictError):
            old.wait()
        current = rebound.output.subscribe()
        current.disconnect()
        self.assertEqual(rebound.inspect().state, "running")
        with self.assertRaises(sdk.ConflictError):
            rebound.restart()
        rebound.kill()
        with self.assertRaises(sdk.NotFoundError):
            rebound.wait(timeout=1)
        self.assertEqual(self.manager.list(), [])

    def test_relative_cwd_preserves_argv_environment_and_native_restart(self):
        cwd = Path(self.directory.name) / "relative '#{pane_id}\n;"
        cwd.mkdir()
        program = cwd / "run=python3;"
        program.symlink_to("/usr/bin/python3")
        handle = self.manager.spawn(["./run=python3;", "-c",
            "import os,sys; assert os.getcwd()==sys.argv[1]; assert os.environ['SDK-TEST']==sys.argv[2]; "
            "assert os.environ['PATH']=='/task-only'; print('relative-screen'); sys.exit(7)",
            str(cwd), "x '$()\n;"], cwd=os.path.relpath(cwd),
            env={"SDK-TEST": "x '$()\n;", "PATH": "/task-only"}, pty=(80, 24))
        self.assertEqual(handle.wait().exit.code, 7)
        rebound = sdk.tmux(sdk.local(), socket_path=self.socket).get(handle.process)
        rebound.restart()
        self.assertEqual(rebound.wait(timeout=5).exit.code, 7)
        self.assertIn(b"relative-screen", rebound.output.snapshot())
        program.unlink()
        cwd.rmdir()
        rebound.restart()
        self.assertEqual(rebound.wait(timeout=5).exit.code, 125)
        rebound.remove()

    def test_capture_is_rendered_screen_and_never_a_log(self):
        handle = self.manager.spawn(["python3", "-c",
            "import sys; sys.stdout.write('old-content\\x1b[2J\\x1b[HOPENSOL_TMUX_NOT_FOUND\\n'); sys.stdout.flush()"],
            pty=(80, 24))
        process = self.manager.get(handle.process)
        self.assertEqual(process.wait(timeout=5).exit.code, 0)
        screen = process.output.snapshot()
        self.assertIn(b"OPENSOL_TMUX_NOT_FOUND", screen)
        self.assertNotIn(b"old-content", screen)
        self.assertNotIn(b"\x1b", screen)
        process.remove()


class AsyncTmuxTests(TmuxFixture, unittest.IsolatedAsyncioTestCase):
    async def test_async_input_snapshot_wait_cancellation_and_disconnect(self):
        manager = aio.tmux(aio.local(), socket_path=self.socket)
        self.assertEqual((await manager.probe()).backend, "tmux")
        handle = await manager.spawn(["python3", "-u", "-c",
            "import os,tty; tty.setraw(0); print('READY',flush=True); data=b''\n"
            "while len(data)<4: data+=os.read(0,4-len(data))\nprint(data.hex(),flush=True)"],
            stdin=True, pty=(80, 24))
        process = manager.get(handle.process)
        for _ in range(100):
            if b"READY" in await process.output.snapshot():
                break
            await asyncio.sleep(.02)
        else:
            self.fail("raw terminal did not become ready")
        reading = asyncio.create_task(handle.next_event())
        await asyncio.sleep(.05)
        reading.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reading
        waiting = asyncio.create_task(process.wait())
        await asyncio.sleep(.05)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual((await process.inspect()).state, "running")
        await handle.aclose()
        replacement = await process.output.subscribe()
        await process.resize(90, 30)
        await process.send_input(b"\x00\xff\nA")
        self.assertEqual((await replacement.wait()).exit.code, 0)
        self.assertIn(b"00ff0a41", await process.output.snapshot())
        self.assertEqual((await process.wait(timeout=5)).exit.code, 0)
        self.assertEqual((await manager.list())[0].process.id, handle.process.id)
        stale = await process.output.subscribe()
        await process.restart()
        with self.assertRaises(sdk.ConflictError):
            await stale.wait()
        await process.kill()
        with self.assertRaises(sdk.NotFoundError):
            await process.inspect()
        await replacement.aclose()
        await stale.aclose()

    async def test_injected_docker_connection_uses_its_native_server_and_files(self):
        backend = sdk.docker(os.environ["OPENSOL_TEST_IMAGE"], command=["sleep", "infinity"], working_dir="/")
        resource = backend.lifecycle.create()
        try:
            backend.lifecycle.start(resource)
            target = backend.connection(resource)
            self.assertEqual(target.exec(["pwd", "-P"]).stdout, b"/\n")
            self.assertTrue(target.exec(["mkdir", "-p", self.directory.name]).success)
            self.assertTrue(target.exec(["tmux", "-S", self.socket, "-f", "/dev/null", "new-session", "-d",
                                         "-s", "remote-user", "sleep", "infinity"]).success)
            manager = aio.tmux(aio.docker_connection(resource.id), socket_path=self.socket)
            self.assertEqual((await manager.probe()).backend, "tmux")
            handle = await manager.spawn(["python3", "-u", "-c",
                "from pathlib import Path; import os,tty; tty.setraw(0); print('remote-ready',flush=True); data=b''\n"
                "while len(data)<4: data+=os.read(0,4-len(data))\n"
                "Path('target-file').write_text(os.environ['TASK']+data.hex()); print('remote-screen',flush=True)"],
                cwd=os.path.relpath(self.directory.name, start="/"),
                env={"TASK": "remote"}, stdin=True, pty=(80, 24))
            process = manager.get(handle.process)
            for _ in range(100):
                if b"remote-ready" in await process.output.snapshot():
                    break
                await asyncio.sleep(.02)
            else:
                self.fail("remote raw terminal did not become ready")
            await process.send_input(b"\x00\xff\nA")
            self.assertEqual((await handle.wait()).exit.code, 0)
            self.assertIn(b"remote-screen", await process.output.snapshot())
            self.assertFalse((Path(self.directory.name) / "target-file").exists())
            self.assertEqual(target.exec(["cat", str(Path(self.directory.name) / "target-file")]).stdout, b"remote00ff0a41")
            self.assertEqual(self.manager.list(), [], "host server at the same socket path is untouched")
            await process.remove()
            await handle.aclose()
        finally:
            backend.lifecycle.delete(resource)
