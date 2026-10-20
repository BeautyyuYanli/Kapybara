"""Installed-wheel resources use real Pueue logs; envd stays a native protocol fixture."""

import asyncio
import subprocess
import tempfile
import unittest

import opensol as sdk
from opensol import aio

import test_sdk


def data(page):
    if any(chunk.channel != "combined" for chunk in page.chunks):
        raise AssertionError("native Pueue output must remain combined")
    return b"".join(chunk.data for chunk in page.chunks)


class ResourceTests(unittest.TestCase):
    stop_daemon = test_sdk.PueueTests.stop_daemon
    running = test_sdk.PueueTests.running

    def setUp(self):
        test_sdk.PueueTests.setUp(self)
        self.manager = sdk.pueue(sdk.local(), config_file=str(self.config),
                                 pueue_directory=str(self.directory))

    def test_bound_resource_pages_restart_controls_and_retention(self):
        info = self.manager.probe()
        self.assertIsInstance(info, sdk.ManagerInfo)
        self.assertTrue(info.client_version.startswith("4."))
        self.assertIsNone(info.server_version)
        process = self.manager.spawn(["python3", "-c",
                                     "import sys; sys.stdout.buffer.write(b'\\x00\\xffout'); sys.exit(7)"])
        handle = process.output.subscribe()

        self.assertIsInstance(process, sdk.Process)
        self.assertIsInstance(process.output, sdk.Output)
        self.assertIsInstance(process.terminal, sdk.Terminal)
        self.assertEqual(process.output.capabilities(), dict(read=True, tail=True, subscribe=True,
                                                           historical_output=True, streaming_output=False))
        self.assertEqual(process.terminal.capabilities(), dict(read=False, tail=False, screen=False))
        self.assertIsInstance(handle, sdk.OutputSubscription)
        self.assertFalse(hasattr(handle, "ref"))
        self.assertFalse(hasattr(handle, "wait"))
        with self.assertRaises(AttributeError):
            process.ref = sdk.ProcessRef("pueue", "other")
        for name in ("output", "terminal"):
            with self.assertRaises(AttributeError):
                setattr(process, name, None)
        self.assertEqual(process.wait(timeout=5).exit.code, 7)
        self.assertEqual(process.inspect().outcome.native_result, "Failed")
        self.assertEqual(test_sdk.collect(handle)[1].native_result, "Failed")
        self.assertIn(process.ref.id, [item.process.id for item in self.manager.list()])
        first = process.output.read(max_bytes=2)
        self.assertIsInstance(first, sdk.OutputPage)
        self.assertIsInstance(first.chunks[0], sdk.OutputChunk)
        self.assertEqual(data(first), b"\x00\xff")
        self.assertFalse(first.at_tail)
        self.assertIs(first.at_head, True)
        saved = sdk.OutputCursor(first.after.value)
        self.assertEqual(str(saved), saved.value)
        second = process.output.read(cursor=saved)
        self.assertEqual(data(second), b"out")
        self.assertTrue(second.at_tail)
        self.assertIs(second.at_head, False)
        self.assertEqual(data(process.output.read(cursor=second.before, direction="backward")), b"\x00\xff")
        tail = process.output.tail(max_bytes=2)
        self.assertEqual(data(tail), b"ut")
        self.assertEqual(data(process.output.read(direction="backward", max_bytes=2)), b"ut")
        self.assertEqual(data(process.output.read(cursor=tail.before, direction="backward", max_bytes=3)), b"\x00\xffo")
        self.assertEqual(data(process.output.read(cursor=first.before, direction="backward")), b"")
        for name, value in (("before", None), ("after", None), ("at_head", False), ("at_tail", False)):
            with self.assertRaises(AttributeError):
                setattr(first, name, value)
        self.assertEqual(data(process.output.read(cursor=saved)), b"out")
        self.assertEqual(data(process.output.read(cursor=second.after)), b"")
        replay, outcome = test_sdk.collect(process.output.subscribe())
        self.assertEqual(test_sdk.combined_output(replay), b"\x00\xffout")
        self.assertEqual(outcome.exit.code, 7)
        self.assertEqual(data(process.output.read(cursor=saved)), b"out", "subscription must not move saved cursors")
        for limit in (0, -1, 1048577, 1 << 100, True, 1.5):
            with self.subTest(max_bytes=limit), self.assertRaises(sdk.InvalidArgumentError):
                process.output.read(max_bytes=limit)
            with self.assertRaises(sdk.InvalidArgumentError):
                process.output.tail(max_bytes=limit)
        with self.assertRaises(sdk.InvalidArgumentError):
            process.output.read(direction="sideways")
        with self.assertRaises(TypeError):
            process.output.read(cursor=sdk.TerminalCursor(saved.value))
        with self.assertRaises(TypeError):
            process.terminal.read(cursor=saved)
        import json
        token = json.loads(saved.value)
        token["representation"] = "terminal"
        with self.assertRaises(sdk.ConflictError):
            process.output.read(cursor=sdk.OutputCursor(json.dumps(token)))
        with self.assertRaises(sdk.InvalidArgumentError):
            process.output.read(cursor=sdk.OutputCursor("bad cursor"))
        subprocess.run(["pueue", "--config", str(self.config), "pause", "--group", "default"],
                       check=True, capture_output=True)
        process.restart()
        self.assertEqual(process.inspect().state, "queued")
        self.assertIsNone(process.inspect().outcome)
        self.assertIsNone(handle.next_event())
        with self.assertRaises(sdk.ConflictError):
            process.output.read(cursor=saved)
        self.assertEqual(data(process.output.read()), b"")
        queued = process.output.tail()
        self.assertEqual((data(queued), queued.at_head, queued.at_tail), (b"", True, True))
        self.assertEqual(queued.before.value, queued.after.value)
        process.start()
        self.assertEqual(process.wait(timeout=5).exit.code, 7)
        self.assertEqual(data(process.output.read()), b"\x00\xffout")
        process.remove()
        self.assertEqual(self.manager.list(), [])
        with self.assertRaises(sdk.NotFoundError):
            process.output.read(cursor=saved)

    def test_failed_to_spawn_completes_without_exit_or_private_error_text(self):
        sdk.local().exec(["pueue", "--config", str(self.config), "pause", "--group", "default"])
        with tempfile.TemporaryDirectory(prefix="opensol-private-cwd-") as cwd:
            process = self.manager.spawn(["true"], cwd=cwd)
            handle = process.output.subscribe()

        process.start()
        outcome = process.wait(timeout=5)
        self.assertIsNone(outcome.exit)
        self.assertEqual(outcome.native_result, "FailedToSpawn")
        self.assertEqual(process.inspect().outcome.native_result, "FailedToSpawn")
        output, completed = test_sdk.collect(handle)
        retained = (self.directory / "task_logs" / f"{process.ref.id}.log").read_bytes()
        self.assertEqual(test_sdk.combined_output(output), retained)
        self.assertIsNone(completed.exit)
        self.assertEqual(completed.native_result, "FailedToSpawn")
        self.assertIsNone(handle.next_event())
        process.remove()

    def test_resources_bind_locally_and_native_pause_kill_remain_explicit(self):
        unavailable = sdk.pueue(sdk.local(), config_file="/no-opensol-config")
        missing = unavailable.get(sdk.ProcessRef("pueue", "123"))
        self.assertEqual(missing.ref.id, "123", "binding must not contact a daemon")
        self.assertTrue(missing.output.capabilities()["subscribe"])
        self.assertFalse(missing.terminal.capabilities()["screen"])
        manager = sdk.pueue(sdk.local(), config_file=str(self.config))
        process = manager.spawn(["sleep", "30"])
        handle = process.output.subscribe()
        self.assertFalse(process.output.capabilities()["read"])
        self.assertFalse(process.terminal.capabilities()["screen"])
        self.running(process.ref)

        with self.assertRaises(sdk.UnsupportedError):
            process.output.tail()
        with self.assertRaises(sdk.UnsupportedError):
            process.terminal.screen()
        process.pause()
        self.assertEqual(process.inspect().state, "paused")
        process.start()
        with self.assertRaises(sdk.ConflictError):
            process.restart()
        with self.assertRaises(sdk.ConflictError):
            process.remove()
        handle.close()
        self.assertEqual(process.inspect().state, "running")
        process.kill()
        outcome = process.wait(timeout=5)
        self.assertIsNone(outcome.exit)
        self.assertEqual(outcome.native_result, "Killed")
        self.assertIsNone(process.inspect().outcome.exit)
        self.assertEqual(process.inspect().outcome.native_result, "Killed")
        replay, completed = test_sdk.collect(process.output.subscribe())
        self.assertEqual(replay, [])
        self.assertEqual(completed.native_result, "Killed")
        process.remove()



class AsyncResourceTests(unittest.IsolatedAsyncioTestCase):
    stop_daemon = test_sdk.PueueTests.stop_daemon

    def setUp(self):
        test_sdk.PueueTests.setUp(self)
        self.manager = aio.pueue(aio.local(), config_file=str(self.config),
                                 pueue_directory=str(self.directory))

    async def test_failed_to_spawn_is_delivered_as_completion_then_ends(self):
        await aio.local().exec(["pueue", "--config", str(self.config), "pause", "--group", "default"])
        with tempfile.TemporaryDirectory(prefix="opensol-private-cwd-") as cwd:
            process = await self.manager.spawn(["true"], cwd=cwd)
            handle = await process.output.subscribe()

        await process.start()
        outcome = await process.wait(timeout=5)
        self.assertIsNone(outcome.exit)
        self.assertEqual(outcome.native_result, "FailedToSpawn")
        self.assertEqual((await process.inspect()).outcome.native_result, "FailedToSpawn")
        from test_async import collect
        output, completed = await collect(handle)
        retained = (self.directory / "task_logs" / f"{process.ref.id}.log").read_bytes()
        self.assertEqual(test_sdk.combined_output(output), retained)
        self.assertIsNone(completed.exit)
        self.assertEqual(completed.native_result, "FailedToSpawn")
        self.assertIsNone(await handle.next_event())
        self.assertIsNone(await handle.next_event())
        await handle.aclose()
        await process.remove()

    async def test_async_resources_share_dtos_and_read_running_output(self):
        self.assertIsInstance(await self.manager.probe(), sdk.ManagerInfo)
        process = await self.manager.spawn(["python3", "-c",
                                          "import sys; sys.stdout.buffer.write(b'\\x00\\xfffirst'); "
                                          "sys.stdout.buffer.flush(); sys.stdin.readline(); "
                                          "sys.stdout.buffer.write(b'last'); sys.exit(7)"])
        handle = await process.output.subscribe()

        self.assertIsInstance(process, aio.Process)
        self.assertIsInstance(handle, aio.OutputSubscription)
        self.assertFalse(hasattr(handle, "ref"))
        self.assertFalse(hasattr(handle, "wait"))
        with self.assertRaises(AttributeError):
            process.ref = sdk.ProcessRef("pueue", "other")
        for name in ("output", "terminal"):
            with self.assertRaises(AttributeError):
                setattr(process, name, None)
        deadline = asyncio.get_running_loop().time() + 5
        while len(data(await process.output.tail(max_bytes=7))) < 7:
            self.assertLess(asyncio.get_running_loop().time(), deadline)
            await asyncio.sleep(.02)
        first = await process.output.read(max_bytes=2)
        self.assertIsInstance(first, sdk.OutputPage)
        self.assertEqual(data(first), b"\x00\xff")
        tail = await process.output.read(cursor=sdk.OutputCursor(first.after.value))
        self.assertEqual(data(tail), b"first")
        self.assertTrue(tail.at_tail)
        self.assertEqual(data(await process.output.tail(max_bytes=3)), b"rst")
        backward = await process.output.read(cursor=tail.before, direction="backward", max_bytes=2)
        self.assertEqual(data(backward), b"\x00\xff")
        self.assertEqual((await process.inspect()).state, "running")
        self.assertIn(process.ref.id, [item.process.id for item in await self.manager.list()])
        with self.assertRaises(sdk.InvalidArgumentError):
            await process.output.read(max_bytes=0)
        await process.pause()
        self.assertEqual((await process.inspect()).state, "paused")
        await process.start()
        await process.send_input(b"finish\n")
        self.assertEqual((await process.wait(timeout=5)).exit.code, 7)
        self.assertEqual(data(await process.output.read(cursor=tail.after)), b"last")
        replay = await process.output.subscribe()
        from test_async import collect
        chunks, outcome = await collect(replay)
        self.assertEqual(test_sdk.combined_output(chunks), b"\x00\xfffirstlast")
        self.assertEqual(outcome.exit.code, 7)
        await replay.aclose()
        self.assertEqual(((await collect(handle))[1]).native_result, "Failed")
        await process.restart()
        # Restarted task still waits for input; an explicit native kill ends it.
        deadline = asyncio.get_running_loop().time() + 5
        while (await process.inspect()).state == "queued":
            self.assertLess(asyncio.get_running_loop().time(), deadline)
            await asyncio.sleep(.02)
        await process.kill()
        outcome = await process.wait(timeout=5)
        self.assertIsNone(outcome.exit)
        self.assertEqual(outcome.native_result, "Killed")
        self.assertIsNone((await process.inspect()).outcome.exit)
        self.assertEqual((await process.inspect()).outcome.native_result, "Killed")
        self.assertIsNone(await handle.next_event())
        await handle.aclose()
        killed_output = await process.output.subscribe()
        chunks, completed = await collect(killed_output)
        self.assertTrue(test_sdk.combined_output(chunks) in (b"", b"\x00\xfffirst"))
        self.assertIsNone(completed.exit)
        self.assertEqual(completed.native_result, "Killed")
        self.assertIsNone(await killed_output.next_event())
        await killed_output.aclose()
        await process.remove()


class EnvdResourceTests(unittest.TestCase):
    setUp = test_sdk.EnvdBindingTests.setUp

    def test_native_list_probe_kill_and_unsupported_resource_operations(self):
        manager = self.native.processes
        info = manager.probe()
        self.assertEqual((info.backend, info.client_version, info.server_version), ("envd", None, None))
        self.assertEqual(manager.list(), [])
        process = manager.spawn(["stdin-program"], stdin=True)
        handle = process.output.subscribe()

        self.assertEqual([info.process.id for info in manager.list()], [process.ref.id])
        self.assertEqual(process.inspect().state, "running")
        self.assertIsNone(process.inspect().outcome)
        for operation in (process.pause, process.start, process.restart, process.remove,
                          process.output.tail, process.output.read, process.terminal.read,
                          process.terminal.tail, process.terminal.screen):
            with self.subTest(operation=operation), self.assertRaises(sdk.UnsupportedError):
                operation()
        process.send_input(b"\x00\xffinput")
        process.kill()
        self.assertEqual(test_sdk.collect(handle)[1].exit.code, 7)
        request = next(message for method, message in self.records if method == "SendSignal")
        self.assertEqual((request.process.pid, request.signal), (int(process.ref.id), 9))
        with self.assertRaises(sdk.NotFoundError):
            process.inspect()


if __name__ == "__main__":
    unittest.main()
