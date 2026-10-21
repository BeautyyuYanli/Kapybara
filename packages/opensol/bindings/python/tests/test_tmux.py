"""Installed async and blocking APIs against caller-owned tmux and real input."""

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


class TmuxFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="opensol-tmux-")
        self.addCleanup(self.directory.cleanup)
        self.socket = str(Path(self.directory.name) / "tmux.sock")
        subprocess.run(["tmux", "-S", self.socket, "-f", "/dev/null", "new-session", "-d",
                        "-s", "user-session", "sleep", "infinity"], check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["tmux", "-N", "-S", self.socket, "kill-server"],
                        capture_output=True)
        self.manager = sdk.ProcessManager(sdk.TmuxConfig(socket_path=self.socket), connection=sdk.Connection(sdk.LocalConfig()))


class TmuxTests(TmuxFixture, unittest.TestCase):
    def test_local_binding_capabilities_validation_and_native_failure(self):
        missing_socket = str(Path(self.directory.name) / "never-started.sock")
        missing = sdk.ProcessManager(sdk.TmuxConfig(socket_path=missing_socket), connection=sdk.Connection(sdk.LocalConfig()))
        capabilities = missing.capabilities()
        self.assertTrue(capabilities["probe"] and capabilities["list"])
        self.assertEqual(capabilities["spawn_modes"], [dict(pty=True, stdin=False), dict(pty=True, stdin=True)])
        control = capabilities["process"]
        for name in ("restart", "remove", "resize", "detached", "reconnect"):
            self.assertTrue(control[name], name)
        for name in ("daemon_restart_recovery", "pause", "start"):
            self.assertFalse(control[name], name)
        self.assertEqual(control["kill"], "remove_task")
        self.assertEqual(control["completion"], "native_task")
        self.assertEqual(control["input"], dict(constraints=dict(content="bytes", max_bytes=8192), close=False))
        self.assertEqual(control["supported_signals"], [])
        bound = missing.get(sdk.ProcessRef("tmux", "opaque-unchecked"))
        self.assertIsNone(bound.capabilities()["input"])
        self.assertFalse(bound.capabilities()["resize"])
        with self.assertRaises(sdk.UnsupportedError):
            bound.send_input(b"input")
        self.assertEqual(bound.output.capabilities(), dict(read=False, tail=False, read_channels=[],
                         subscribe=dict(initial=None, reconnect=dict(channels=[], historical_output=False,
                                        streaming_output=False, completion="native_task"))))
        self.assertEqual(bound.terminal.capabilities(), dict(read=False, tail=True, screen=True))
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
                sdk.ProcessManager(sdk.TmuxConfig(**options), connection=sdk.Connection(sdk.LocalConfig()))
        absent = sdk.ProcessManager(sdk.TmuxConfig(socket_path=self.socket, program="/no-private-tmux"), connection=sdk.Connection(sdk.LocalConfig()))
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
        process = self.manager.spawn(["python3", "-u", "-c",
            "import os,tty,hashlib; tty.setraw(0); print('READY',flush=True); data=b''\n"
            "while len(data)<8192: data+=os.read(0,8192-len(data))\n"
            "print(hashlib.sha256(data).hexdigest(), os.get_terminal_size(0),flush=True); raise SystemExit(7)"],
            stdin=True, pty=(101, 29))
        handle = process.output.subscribe()

        self.assertIs(process.ref.pty, True)
        self.assertEqual(process.capabilities(), self.manager.capabilities()["process"])
        unknown_mode = self.manager.get(sdk.ProcessRef("tmux", process.ref.id))
        self.assertIsNone(unknown_mode.capabilities()["input"])
        self.assertFalse(unknown_mode.capabilities()["resize"])
        self.assertEqual(handle.capabilities(), process.output.capabilities()["subscribe"]["reconnect"])
        for _ in range(100):
            if "READY" in "\n".join(process.terminal.screen().lines):
                break
            time.sleep(.02)
        else:
            self.fail("raw terminal did not become ready")
        for call in (process.output.tail, process.output.read, process.pause, process.start,
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
        screen = process.terminal.screen()
        self.assertIsInstance(screen, sdk.TerminalScreen)
        self.assertEqual((screen.cols, screen.rows, len(screen.lines)), (107, 33, 33))
        output = "\n".join(screen.lines)
        self.assertIn(hashlib.sha256(payload).hexdigest(), output)
        self.assertIn("columns=107, lines=33", output)
        self.assertEqual(process.wait(timeout=5).exit.code, 7)
        self.assertEqual(self.manager.list()[0].process.id, process.ref.id)
        process.remove()
        with self.assertRaises(sdk.NotFoundError):
            "\n".join(process.terminal.screen().lines)

    def test_pane_completion_precedes_os_exit_and_delivered_outcome_stays_fixed(self):
        ready = Path(self.directory.name) / "pid"
        release = Path(self.directory.name) / "release"

        def kill_child():
            # Ignoring SIGHUP lets the child survive PTY EOF and server teardown.
            # Kill it explicitly on a timeout or assertion failure, before directory cleanup.
            if ready.exists():
                subprocess.run(["kill", "-KILL", ready.read_text()], capture_output=True, timeout=5)

        self.addCleanup(kill_child)
        process = self.manager.spawn([
            "python3", "-c",
            "import os,signal,sys,time; from pathlib import Path\n"
            "ready,release=map(Path,sys.argv[1:])\n"
            "temporary=ready.with_suffix('.tmp')\n"
            "temporary.write_text(str(os.getpid())); temporary.replace(ready)\n"
            "signal.signal(signal.SIGHUP,signal.SIG_IGN)\n"
            "os.closerange(0,1024)\n"
            "while not release.exists(): time.sleep(.01)\n"
            "os._exit(23)\n",
            str(ready), str(release),
        ], pty=(80, 24))
        subscription = process.output.subscribe()
        self.addCleanup(subscription.close)
        deadline = time.monotonic() + 5
        while not ready.exists():
            self.assertLess(time.monotonic(), deadline, "native process did not publish its PID")
            time.sleep(.01)
        pid = int(ready.read_text())

        # The closed barrier keeps the OS process alive throughout all three completion APIs.
        outcome = process.wait(timeout=2)
        info = process.inspect()
        event = subscription.next_event(timeout=2)
        os.kill(pid, 0)
        self.assertEqual(info.state, "completed")
        for completed in (outcome, info.outcome, event.outcome):
            self.assertIsNone(completed.exit)
            self.assertEqual(completed.native_result, "Dead")

        release.touch()
        deadline = time.monotonic() + 5
        while (later := process.wait(timeout=2)).exit is None:
            self.assertLess(time.monotonic(), deadline, "native OS exit status did not arrive")
            time.sleep(.01)
        self.assertEqual(later.exit.code, 23)
        self.assertIsNone(event.outcome.exit)
        self.assertEqual(event.outcome.native_result, "Dead")
        self.assertIsNone(subscription.next_event(timeout=1))
        process.remove()

    def test_restart_reconnection_execution_binding_and_external_respawn(self):
        marker = str(Path(self.directory.name) / "execution")
        process = self.manager.spawn(["python3", "-c",
            "from pathlib import Path; import sys; p=Path(sys.argv[1]); old=p.exists(); p.touch(); "
            "print('new-screen' if old else 'old-screen'); sys.exit(9 if old else 7)", marker], pty=(80, 24))
        handle = process.output.subscribe()

        self.assertEqual(handle.next_event(timeout=5).outcome.exit.code, 7)
        original = process.output.subscribe()
        process.restart()
        with self.assertRaises(sdk.ConflictError):
            original.next_event(timeout=5)
        self.assertIsNone(handle.next_event())
        self.assertEqual(process.wait(timeout=5).exit.code, 9)
        self.assertIn("new-screen", "\n".join(process.terminal.screen().lines))
        rebound = sdk.ProcessManager(sdk.TmuxConfig(socket_path=self.socket), connection=sdk.Connection(sdk.LocalConfig())).get(
            sdk.ProcessRef("tmux", process.ref.id))
        self.assertEqual(rebound.output.subscribe().next_event(timeout=5).outcome.exit.code, 9)
        old = rebound.output.subscribe()
        pane = json.loads(process.ref.id)["pane"]
        subprocess.run(["tmux", "-N", "-S", self.socket, "respawn-pane", "-t", pane,
                        "sleep", "30"], check=True, capture_output=True)
        with self.assertRaises(sdk.ConflictError):
            old.next_event(timeout=5).outcome
        current = rebound.output.subscribe()
        current.close()
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
        process = self.manager.spawn(["./run=python3;", "-c",
            "import os,sys; assert os.getcwd()==sys.argv[1]; assert os.environ['SDK-TEST']==sys.argv[2]; "
            "assert os.environ['PATH']=='/task-only'; print('relative-screen'); sys.exit(7)",
            str(cwd), "x '$()\n;"], cwd=os.path.relpath(cwd),
            env={"SDK-TEST": "x '$()\n;", "PATH": "/task-only"}, pty=(80, 24))
        handle = process.output.subscribe()
        self.assertEqual(handle.next_event(timeout=5).outcome.exit.code, 7)
        rebound = sdk.ProcessManager(sdk.TmuxConfig(socket_path=self.socket), connection=sdk.Connection(sdk.LocalConfig())).get(process.ref)
        rebound.restart()
        self.assertEqual(rebound.wait(timeout=5).exit.code, 7)
        self.assertIn("relative-screen", "\n".join(rebound.terminal.screen().lines))
        program.unlink()
        cwd.rmdir()
        rebound.restart()
        self.assertEqual(rebound.wait(timeout=5).exit.code, 125)
        rebound.remove()

    def test_capture_is_rendered_screen_and_never_a_log(self):
        process = self.manager.spawn(["python3", "-c",
            "import sys; sys.stdout.write('old-content\\x1b[2J\\x1b[HOPENSOL_TMUX_NOT_FOUND\\n'); sys.stdout.flush()"],
            pty=(80, 24))
        handle = process.output.subscribe()

        self.assertEqual(process.wait(timeout=5).exit.code, 0)
        screen = "\n".join(process.terminal.screen().lines)
        self.assertIn("OPENSOL_TMUX_NOT_FOUND", screen)
        self.assertNotIn("old-content", screen)
        self.assertNotIn("\x1b", screen)
        process.remove()

    def test_native_history_bounds_active_alternate_grid_and_exact_text_lines(self):
        process = self.manager.spawn(["python3", "-u", "-c",
            "import os,tty; tty.setraw(0); "
            "os.write(1, ''.join(f'line-{i:03}  \\r\\n' for i in range(50)).encode()); "
            "os.write(1, 'wide:界 é sep:\u2028end  READY'.encode()); os.read(0,1); "
            "os.write(1,b'\\x1b[?1049hALT-READY'); os.read(0,1); "
            "os.write(1,b'\\x1b[?1049l'); os.read(0,1)"], stdin=True, pty=(40, 6))
        handle = process.output.subscribe()
        pane = json.loads(process.ref.id)["pane"]

        def native(*args):
            return subprocess.run(["tmux", "-u", "-N", "-S", self.socket, *args],
                                  check=True, capture_output=True).stdout

        def wait_text(needle):
            for _ in range(200):
                screen = process.terminal.screen()
                if needle in "\n".join(screen.lines):
                    return screen
                time.sleep(.01)
            self.fail(f"terminal never showed {needle}")

        screen = wait_text("READY")
        self.assertEqual((screen.cols, screen.rows, len(screen.lines)), (40, 6, 6))
        expected = native("capture-pane", "-p", "-N", "-t", pane).decode().removesuffix("\n").split("\n")
        self.assertEqual(screen.lines, expected)
        self.assertTrue(any("\u2028end" in line for line in screen.lines), "only LF separates rendered lines")
        self.assertTrue(any(line.endswith("  ") for line in screen.lines))
        page = process.terminal.tail(lines=3)
        expected = native("capture-pane", "-p", "-N", "-t", pane, "-S", "-3", "-E", "-1").decode().removesuffix("\n").split("\n")
        self.assertEqual(page.lines, expected)
        self.assertEqual((len(page.lines), page.before, page.after, page.at_head, page.at_tail), (3, None, None, False, True))
        self.assertTrue(process.terminal.tail(lines=200).at_head)
        for size in (0, -1, 10001, 1 << 100, True, 1.5):
            with self.assertRaises(sdk.InvalidArgumentError):
                process.terminal.tail(lines=size)
            with self.assertRaises(sdk.InvalidArgumentError):
                process.terminal.read(lines=size)
        with self.assertRaises(sdk.UnsupportedError):
            process.terminal.read(direction="backward")

        native("copy-mode", "-t", pane)
        native("send-keys", "-t", pane, "-X", "history-top")
        self.assertEqual(process.terminal.screen().lines, screen.lines, "query reads the active grid through copy mode")
        native("send-keys", "-t", pane, "-X", "cancel")
        process.send_input(b"a")
        alternate = wait_text("ALT-READY")
        self.assertNotIn("line-", "\n".join(alternate.lines))
        page = process.terminal.tail(lines=5)
        self.assertEqual((page.lines, page.before, page.after, page.at_head, page.at_tail), ([], None, None, True, True))
        process.send_input(b"b")
        self.assertEqual(wait_text("wide:").lines, screen.lines)
        native("clear-history", "-t", pane)
        self.assertEqual(process.terminal.tail().lines, [])
        self.assertEqual(process.terminal.screen().lines, screen.lines)
        process.send_input(b"c")
        handle.next_event(timeout=5).outcome
        process.remove()


class AsyncTmuxTests(TmuxFixture, unittest.IsolatedAsyncioTestCase):
    async def test_async_input_snapshot_wait_cancellation_and_close(self):
        manager = sdk.AsyncProcessManager(sdk.TmuxConfig(socket_path=self.socket), connection=sdk.AsyncConnection(sdk.LocalConfig()))
        self.assertEqual((await manager.probe()).backend, "tmux")
        process = await manager.spawn(["python3", "-u", "-c",
            "import os,tty; tty.setraw(0); print('READY',flush=True); data=b''\n"
            "while len(data)<4: data+=os.read(0,4-len(data))\nprint(data.hex(),flush=True)"],
            stdin=True, pty=(80, 24))
        handle = await process.output.subscribe()

        for _ in range(100):
            if "READY" in "\n".join((await process.terminal.screen()).lines):
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
        self.assertEqual(((await replacement.next_event(timeout=5)).outcome).exit.code, 0)
        self.assertIn("00ff0a41", "\n".join((await process.terminal.screen()).lines))
        self.assertEqual((await process.wait(timeout=5)).exit.code, 0)
        self.assertEqual((await manager.list())[0].process.id, process.ref.id)
        stale = await process.output.subscribe()
        await process.restart()
        with self.assertRaises(sdk.ConflictError):
            (await stale.next_event(timeout=5)).outcome
        await process.kill()
        with self.assertRaises(sdk.NotFoundError):
            await process.inspect()
        await replacement.aclose()
        await stale.aclose()

    async def test_injected_docker_connection_uses_its_native_server_and_files(self):
        backend = sdk.Backend(sdk.DockerConfig())
        resource = sdk.SandboxManager(backend).create(sdk.DockerSandboxSpec(os.environ["OPENSOL_TEST_IMAGE"],
                                                                    command=["sleep", "infinity"], working_dir="/"))
        try:
            resource.start()
            target = sdk.Connection(backend, target=resource.ref)
            self.assertEqual(target.exec(["pwd", "-P"]).stdout, b"/\n")
            self.assertTrue(target.exec(["mkdir", "-p", self.directory.name]).success)
            self.assertTrue(target.exec(["tmux", "-S", self.socket, "-f", "/dev/null", "new-session", "-d",
                                         "-s", "remote-user", "sleep", "infinity"]).success)
            manager = sdk.AsyncProcessManager(sdk.TmuxConfig(socket_path=self.socket),
                         connection=sdk.AsyncConnection(backend, target=resource.ref))
            self.assertEqual((await manager.probe()).backend, "tmux")
            process = await manager.spawn(["python3", "-u", "-c",
                "from pathlib import Path; import os,tty; tty.setraw(0); print('remote-ready',flush=True); data=b''\n"
                "while len(data)<4: data+=os.read(0,4-len(data))\n"
                "Path('target-file').write_text(os.environ['TASK']+data.hex()); print('remote-screen',flush=True)"],
                cwd=os.path.relpath(self.directory.name, start="/"),
                env={"TASK": "remote"}, stdin=True, pty=(80, 24))
            handle = await process.output.subscribe()

            for _ in range(100):
                if "remote-ready" in "\n".join((await process.terminal.screen()).lines):
                    break
                await asyncio.sleep(.02)
            else:
                self.fail("remote raw terminal did not become ready")
            await process.send_input(b"\x00\xff\nA")
            self.assertEqual(((await handle.next_event(timeout=5)).outcome).exit.code, 0)
            self.assertIn("remote-screen", "\n".join((await process.terminal.screen()).lines))
            self.assertFalse((Path(self.directory.name) / "target-file").exists())
            self.assertEqual(target.exec(["cat", str(Path(self.directory.name) / "target-file")]).stdout, b"remote00ff0a41")
            self.assertEqual(self.manager.list(), [], "host server at the same socket path is untouched")
            await process.remove()
            await handle.aclose()
        finally:
            resource.delete()
