"""Installed-wheel resources use real Pueue logs; envd stays a native protocol fixture."""

import asyncio
import subprocess
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
        self.assertTrue(self.manager.capabilities()["output_resource"])
        handle = self.manager.spawn(["python3", "-c",
                                     "import sys; sys.stdout.buffer.write(b'\\x00\\xffout'); sys.exit(7)"])
        process = self.manager.get(handle.process)
        self.assertIsInstance(process, sdk.Process)
        self.assertEqual(process.ref.id, handle.process.id)
        self.assertEqual(process.wait(timeout=5).code, 7)
        self.assertEqual(process.inspect().native_result, "Failed")
        self.assertIn(process.ref.id, [item.process.id for item in self.manager.list()])
        output = process.output
        self.assertIsInstance(output, sdk.ProcessOutput)
        info = output.stat()
        self.assertIsInstance(info, sdk.OutputInfo)
        self.assertEqual((info.channels, info.size), (["combined"], 5))
        self.assertIsNotNone(info.version)
        first = output.read(limit=2)
        self.assertIsInstance(first, sdk.OutputPage)
        self.assertIsInstance(first.chunks[0], sdk.OutputChunk)
        self.assertEqual(data(first), b"\x00\xff")
        self.assertFalse(first.at_end)
        saved = sdk.OutputCursor(first.next_cursor.value)
        self.assertEqual(str(saved), saved.value)
        second = output.read(cursor=saved)
        self.assertEqual(data(second), b"out")
        self.assertTrue(second.at_end)
        self.assertEqual(data(output.read(cursor=saved)), b"out")
        self.assertEqual(data(output.read(cursor=second.next_cursor)), b"")
        replay, exit = test_sdk.collect(output.subscribe())
        self.assertEqual(test_sdk.combined_output(replay), b"\x00\xffout")
        self.assertEqual(exit.code, 7)
        self.assertEqual(data(output.read(cursor=saved)), b"out", "subscription must not move saved cursors")
        for limit in (0, -1, 1048577):
            with self.subTest(limit=limit), self.assertRaises(sdk.InvalidArgumentError):
                output.read(limit=limit)
        with self.assertRaises(sdk.InvalidArgumentError):
            output.read(cursor=sdk.OutputCursor("bad cursor"))
        subprocess.run(["pueue", "--config", str(self.config), "pause", "--group", "default"],
                       check=True, capture_output=True)
        process.restart()
        self.assertEqual(process.inspect().state, "queued")
        with self.assertRaises(sdk.ConflictError):
            output.read(cursor=saved)
        self.assertEqual(data(output.read()), b"")
        self.assertEqual((output.stat().size, output.stat().version), (0, None))
        process.start()
        self.assertEqual(process.wait(timeout=5).code, 7)
        self.assertEqual(data(output.read()), b"\x00\xffout")
        process.remove()
        self.assertEqual(self.manager.list(), [])
        with self.assertRaises(sdk.NotFoundError):
            output.read(cursor=saved)

    def test_resources_bind_locally_and_native_pause_kill_remain_explicit(self):
        unavailable = sdk.pueue(sdk.local(), config_file="/no-opensol-config")
        missing = unavailable.get(sdk.ProcessRef("pueue", "123"))
        self.assertEqual(missing.ref.id, "123", "binding must not contact a daemon")
        manager = sdk.pueue(sdk.local(), config_file=str(self.config))
        self.assertFalse(manager.capabilities()["output_resource"])
        handle = manager.spawn(["sleep", "30"])
        self.running(handle.process)
        process = manager.get(handle.process)
        with self.assertRaises(sdk.UnsupportedError):
            process.output.stat()
        process.pause()
        self.assertEqual(process.inspect().state, "paused")
        process.start()
        with self.assertRaises(sdk.ConflictError):
            process.restart()
        with self.assertRaises(sdk.ConflictError):
            process.remove()
        handle.disconnect()
        self.assertEqual(process.inspect().state, "running")
        process.kill()
        with self.assertRaisesRegex(sdk.BackendError, "Killed"):
            process.wait(timeout=5)
        self.assertIsNone(process.inspect().exit)
        self.assertEqual(process.inspect().native_result, "Killed")
        process.remove()


class AsyncResourceTests(unittest.IsolatedAsyncioTestCase):
    stop_daemon = test_sdk.PueueTests.stop_daemon

    def setUp(self):
        test_sdk.PueueTests.setUp(self)
        self.manager = aio.pueue(aio.local(), config_file=str(self.config),
                                 pueue_directory=str(self.directory))

    async def test_async_resources_share_dtos_and_read_running_output(self):
        self.assertIsInstance(await self.manager.probe(), sdk.ManagerInfo)
        handle = await self.manager.spawn(["python3", "-c",
                                          "import sys; sys.stdout.buffer.write(b'\\x00\\xfffirst'); "
                                          "sys.stdout.buffer.flush(); sys.stdin.readline(); "
                                          "sys.stdout.buffer.write(b'last'); sys.exit(7)"])
        process = self.manager.get(handle.process)
        self.assertIsInstance(process, aio.Process)
        self.assertEqual(process.ref.id, handle.process.id)
        deadline = asyncio.get_running_loop().time() + 5
        while (await process.output.stat()).size < 7:
            self.assertLess(asyncio.get_running_loop().time(), deadline)
            await asyncio.sleep(.02)
        self.assertIsInstance(process.output, aio.ProcessOutput)
        first = await process.output.read(limit=2)
        self.assertIsInstance(first, sdk.OutputPage)
        self.assertEqual(data(first), b"\x00\xff")
        tail = await process.output.read(cursor=sdk.OutputCursor(first.next_cursor.value))
        self.assertEqual(data(tail), b"first")
        self.assertTrue(tail.at_end)
        self.assertEqual((await process.inspect()).state, "running")
        self.assertIn(process.ref.id, [item.process.id for item in await self.manager.list()])
        with self.assertRaises(sdk.InvalidArgumentError):
            await process.output.read(limit=0)
        await process.pause()
        self.assertEqual((await process.inspect()).state, "paused")
        await process.start()
        await process.send_input(b"finish\n")
        self.assertEqual((await process.wait(timeout=5)).code, 7)
        self.assertEqual(data(await process.output.read(cursor=tail.next_cursor)), b"last")
        replay = await process.output.subscribe()
        from test_async import collect
        chunks, exit = await collect(replay)
        self.assertEqual(test_sdk.combined_output(chunks), b"\x00\xfffirstlast")
        self.assertEqual(exit.code, 7)
        await replay.aclose()
        await handle.aclose()
        await process.restart()
        # Restarted task still waits for input; an explicit native kill ends it.
        deadline = asyncio.get_running_loop().time() + 5
        while (await process.inspect()).state == "queued":
            self.assertLess(asyncio.get_running_loop().time(), deadline)
            await asyncio.sleep(.02)
        await process.kill()
        with self.assertRaisesRegex(sdk.BackendError, "Killed"):
            await process.wait(timeout=5)
        self.assertIsNone((await process.inspect()).exit)
        self.assertEqual((await process.inspect()).native_result, "Killed")
        killed_output = await process.output.subscribe()
        with self.assertRaisesRegex(sdk.BackendError, "Killed"):
            await killed_output.next_event()
        await killed_output.aclose()
        await process.remove()


class EnvdResourceTests(unittest.TestCase):
    setUp = test_sdk.EnvdBindingTests.setUp

    def test_native_list_probe_kill_and_unsupported_resource_operations(self):
        manager = self.native.processes
        info = manager.probe()
        self.assertEqual((info.backend, info.client_version, info.server_version), ("envd", None, None))
        self.assertEqual(manager.list(), [])
        handle = manager.spawn(["stdin-program"], stdin=True)
        process = manager.get(handle.process)
        self.assertEqual([info.process.id for info in manager.list()], [process.ref.id])
        self.assertEqual(process.inspect().state, "running")
        self.assertIsNone(process.inspect().native_result)
        for operation in (process.pause, process.start, process.restart, process.remove,
                          process.output.stat, process.output.read):
            with self.subTest(operation=operation), self.assertRaises(sdk.UnsupportedError):
                operation()
        process.send_input(b"\x00\xffinput")
        process.kill()
        self.assertEqual(handle.wait().code, 7)
        request = next(message for method, message in self.records if method == "SendSignal")
        self.assertEqual((request.process.pid, request.signal), (int(process.ref.id), 9))
        with self.assertRaises(sdk.NotFoundError):
            process.inspect()


if __name__ == "__main__":
    unittest.main()
