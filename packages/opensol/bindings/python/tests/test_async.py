"""Async contracts against real runtimes and the same native protocol fixtures.

The delivery-race test instruments only the Python/Rust awaitable boundary: the
event and stream still come from the actual native client and ConnectRPC server.
"""

import asyncio
import inspect
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import grpc
import opensol as sdk
from opensol import aio
import test_sdk


async def collect(handle):
    output = []
    async for event in handle:
        if event.outcome is not None:
            return output, event.outcome
        output.append((event.channel, event.data))
    raise AssertionError("stream ended without native completion")


async def cancel_before_delivery(handle):
    """Cancel after a real Rust result is ready, before its Python await resumes."""
    native = handle._native
    observed = []
    reading = None

    class CancelOnDelivery:
        def _reserve(self, **kwargs):
            future = native._reserve(**kwargs)

            def cancel_before_task_resumes(completed):
                # Added before Task's await callback, at the binding boundary.
                observed.append(completed.exception() or completed.result())
                reading.cancel()

            future.add_done_callback(cancel_before_task_resumes)
            return future

    handle._native = CancelOnDelivery()
    reading = asyncio.create_task(handle.next_event())
    try:
        try:
            await asyncio.wait_for(reading, 2)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("delivery-boundary cancellation did not reach the reader")
    finally:
        handle._native = native
    if len(observed) != 1:
        raise AssertionError("the native result was not observed before cancelling")
    return observed[0]


class CoroutineContractTests(unittest.TestCase):
    def test_public_io_and_network_factories_are_coroutine_functions(self):
        # Coroutine functions are the public scheduling contract, including methods
        # that can reject unsupported operations when their coroutine is executed.
        methods = {
            aio.Connection: ("exec", "shell"),
            aio.Lifecycle: ("create", "inspect", "start", "stop", "delete", "fork"),
            aio.ProcessManager: ("probe", "list", "spawn"),
            aio.Process: ("inspect", "wait", "pause", "start", "restart", "kill", "remove",
                          "signal", "send_input", "close_stdin", "resize"),
            aio.ProcessOutput: ("stat", "read", "snapshot", "subscribe"),
            aio.ProcessHandle: ("next_event", "wait", "aclose", "__anext__"),
            aio.E2b: ("envd",),
        }
        for owner, names in methods.items():
            for name in names:
                with self.subTest(owner=owner.__name__, name=name):
                    self.assertTrue(inspect.iscoroutinefunction(getattr(owner, name)))
        for factory in (aio.sandboxd, aio.openshell, aio.openshell_connection):
            self.assertTrue(inspect.iscoroutinefunction(factory))
        for factory in (aio.local, aio.ssh, aio.docker, aio.docker_connection, aio.pueue, aio.tmux,
                        aio.envd, aio.e2b, aio.agent_sandbox):
            self.assertFalse(inspect.iscoroutinefunction(factory))

    def test_command_coroutine_can_be_created_before_the_loop(self):
        command = aio.local().exec(["printf", "%s", "scheduled"])

        async def run():
            return await asyncio.create_task(command)

        self.assertEqual(asyncio.run(run()).stdout, b"scheduled")


class AsyncConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_wait_for_scheduling_and_cancel_before_start_has_no_effect(self):
        connection = aio.local()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            for operation in (
                lambda: connection.exec(
                    ["python3", "-c", "from pathlib import Path; Path('marker').write_text('ran')"],
                    cwd=directory,
                ),
                lambda: connection.shell("printf ran > marker", cwd=directory),
            ):
                command = operation()
                self.addCleanup(command.close)
                self.assertTrue(inspect.iscoroutine(command))
                await asyncio.sleep(.05)
                self.assertFalse(marker.exists(), "calling an I/O method must not start it")
                self.assertTrue((await asyncio.create_task(command)).success)
                self.assertEqual(marker.read_bytes(), b"ran")
                marker.unlink()
                cancelled = asyncio.create_task(operation())
                cancelled.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cancelled
                await asyncio.sleep(.05)
                self.assertFalse(marker.exists(), "cancelling before execution must avoid I/O")

    async def test_native_exec_yields_loop_and_preserves_bytes_errors_and_timeout(self):
        connection = aio.local()
        future = asyncio.create_task(
            connection.exec(
                ["python3", "-c", "import sys,time; time.sleep(.15); "
                 "sys.stdout.buffer.write(sys.stdin.buffer.read()); "
                 "sys.stderr.buffer.write(b'\\xfferr'); sys.exit(7)"],
                stdin=b"\x00\xffnative", timeout=3,
            )
        )
        await asyncio.sleep(0.03)
        self.assertFalse(future.done(), "native exec must let the caller's loop progress")
        result = await future
        self.assertIsInstance(result, sdk.ExecResult)
        self.assertEqual((result.stdout, result.stderr, result.exit_code),
                         (b"\x00\xffnative", b"\xfferr", 7))
        self.assertEqual((await connection.shell("printf '%s' async")).stdout, b"async")
        with self.assertRaises(sdk.NotFoundError) as caught:
            await connection.exec(["/no-such-opensol-program"])
        self.assertEqual((caught.exception.backend, caught.exception.operation), ("local", "exec"))
        with self.assertRaises(sdk.TimeoutError):
            await connection.exec(["sleep", "2"], timeout=0.02)
        future = asyncio.create_task(connection.exec(["sleep", "2"]))
        future.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await future
        self.assertTrue((await connection.exec(["true"])).success)


class AsyncPueueTests(unittest.IsolatedAsyncioTestCase):
    stop_daemon = test_sdk.PueueTests.stop_daemon

    def setUp(self):
        test_sdk.PueueTests.setUp(self)
        self.manager = aio.pueue(aio.local(), config_file=str(self.config))

    async def running(self, process):
        for _ in range(100):
            if (await self.manager.get(process).inspect()).state == "running":
                return
            await asyncio.sleep(0.03)
        self.fail("native Pueue task did not start")

    async def test_cancelled_reads_timeouts_and_input_keep_task_and_subscription(self):
        manager = self.manager
        self.assertEqual(set(manager.capabilities()["supported_signals"]), {2, 9, 15})
        handle = await manager.spawn(
            ["/bin/sh", "-c", "read -r value; printf '%s' \"$value\"; exit 7"], stdin=True,
        )
        await self.running(handle.process)
        reading = asyncio.create_task(handle.next_event())
        await asyncio.sleep(0.03)
        reading.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reading
        waiting = asyncio.create_task(handle.wait())
        await asyncio.sleep(0.03)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        with self.assertRaises(sdk.TimeoutError):
            await handle.next_event(timeout=0.02)
        with self.assertRaises(sdk.TimeoutError):
            await manager.get(handle.process).wait(timeout=0.02)
        self.assertEqual((await manager.get(handle.process).inspect()).state, "running")
        with self.assertRaises(sdk.UnsupportedError):
            await manager.get(handle.process).close_stdin()
        with self.assertRaises(sdk.UnsupportedError):
            await manager.get(handle.process).resize(80, 24)
        restored = sdk.ProcessRef("pueue", handle.process.id)
        await manager.get(restored).send_input(b"hello '$()\n")
        output, outcome = await collect(handle)
        self.assertTrue(all(channel == "combined" for channel, _ in output))
        self.assertEqual(b"".join(data for _, data in output), b"hello '$()")
        self.assertIsInstance(outcome, sdk.ProcessOutcome)
        self.assertEqual((outcome.exit.code, outcome.native_result), (7, "Failed"))
        self.assertIsNone(await handle.next_event())
        self.assertEqual((await handle.wait()).exit.code, 7)
        self.assertEqual((await manager.get(handle.process).wait(timeout=2)).exit.code, 7)
        restored = sdk.ProcessRef(handle.process.backend, handle.process.id)
        replay = await manager.get(restored).output.subscribe()
        replay_output, replay_outcome = await collect(replay)
        self.assertEqual(b"".join(data for _, data in replay_output), b"hello '$()")
        self.assertEqual(replay_outcome.exit.code, 7)
        await replay.aclose()

    async def test_unconsumed_subscriptions_reject_new_execution_and_keep_completed_cache(self):
        script = self.directory / "command.sh"
        script.write_text("printf 7; exit 7")
        initial = await self.manager.spawn(["sh", str(script)])
        process = self.manager.get(initial.process)
        self.assertEqual((await process.wait(timeout=5)).exit.code, 7)
        replay = await process.output.subscribe()
        cached = await process.output.subscribe()
        output, outcome = await collect(cached)
        self.assertEqual(b"".join(data for _, data in output), b"7")
        self.assertEqual(outcome.exit.code, 7)
        script.write_text("printf 9; exit 9")
        await asyncio.create_task(process.restart())
        self.assertEqual((await asyncio.create_task(process.wait(timeout=5))).exit.code, 9)
        for handle in (initial, replay):
            with self.assertRaises(sdk.ConflictError):
                await handle.next_event(timeout=5)
            await handle.aclose()
        self.assertEqual((await cached.wait()).exit.code, 7)
        await cached.aclose()
        fresh = await process.output.subscribe()
        output, outcome = await collect(fresh)
        self.assertEqual(b"".join(data for _, data in output), b"9")
        self.assertEqual(outcome.exit.code, 9)
        await fresh.aclose()
        removed = await process.output.subscribe()
        await process.remove()
        with self.assertRaises(sdk.NotFoundError):
            await removed.next_event(timeout=5)
        await removed.aclose()

    async def test_cancel_before_delivery_preserves_native_only_completion_and_cache(self):
        handle = await self.manager.spawn(["sleep", "30"])
        await self.running(handle.process)
        await self.manager.get(handle.process).signal(9)
        # Isolate delivery cancellation from the native CLI's two-second wait polling.
        await self.manager.get(handle.process).wait(timeout=5)
        observed = await cancel_before_delivery(handle)
        self.assertIsNone(observed.outcome.exit)
        self.assertEqual(observed.outcome.native_result, "Killed")
        event = await handle.next_event(timeout=2)
        self.assertIsNone(event.outcome.exit)
        self.assertEqual(event.outcome.native_result, "Killed")
        self.assertIsNone(event.channel)
        self.assertEqual(event.data, b"")
        self.assertIsNone(await handle.next_event())
        for _ in range(2):
            outcome = await handle.wait()
            self.assertIsNone(outcome.exit)
            self.assertEqual(outcome.native_result, "Killed")
        await handle.aclose()

    async def test_aclose_wakes_reader_without_signalling_and_is_idempotent(self):
        handle = await self.manager.spawn(["sleep", "10"])
        await self.running(handle.process)
        reading = asyncio.create_task(handle.next_event())
        await asyncio.sleep(0.03)
        await handle.aclose()
        self.assertIsNone(await asyncio.wait_for(reading, 2))
        await handle.aclose()
        self.assertEqual((await self.manager.get(handle.process).inspect()).state, "running")
        with self.assertRaises(sdk.TransportError):
            await handle.wait()
        await self.manager.get(handle.process).signal(9)
        outcome = await self.manager.get(handle.process).wait(timeout=3)
        self.assertIsNone(outcome.exit)
        self.assertEqual(outcome.native_result, "Killed")
        self.assertIsNone((await self.manager.get(handle.process).inspect()).outcome.exit)


class AsyncEnvdTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_sdk.EnvdBindingTests.setUp(self)
        self.native = aio.envd(self.endpoint)

    async def test_initial_stream_and_additional_subscription_use_start_and_connect(self):
        manager = self.native.processes
        initial = await manager.spawn(["pty-program"], stdin=True, pty=(80, 24))
        process = manager.get(initial.process)
        self.assertEqual((await initial.next_event(timeout=2)).data, b"\x00\xffterminal")
        self.assertEqual([method for method, _ in self.records], ["Start"])
        restored = sdk.ProcessRef("envd", process.ref.id, pty=True)
        offline = aio.envd("http://127.0.0.1:1").processes.get(restored)
        self.assertIsInstance(offline.output, aio.ProcessOutput)
        replay = await manager.get(restored).output.subscribe()
        self.assertEqual((replay.process.id, replay.process.pty), (restored.id, True))
        self.assertEqual((await replay.next_event(timeout=2)).data, b"\x00\xffterminal")
        await process.send_input(b"\x00\xffreconnected\x04")
        original_output, original_outcome = await collect(initial)
        replay_output, replay_outcome = await collect(replay)
        self.assertEqual(original_output, replay_output)
        self.assertTrue(all(channel == "terminal" for channel, _ in replay_output))
        self.assertEqual(b"".join(data for _, data in replay_output), b"\x00\xffreconnected\x04")
        self.assertEqual((original_outcome.exit.code, replay_outcome.exit.code), (7, 7))
        self.assertEqual((await replay.wait()).exit.code, 7)
        connected = [message for method, message in self.records if method == "Connect"]
        self.assertEqual([message.process.pid for message in connected], [int(restored.id)])
        await initial.aclose()
        await replay.aclose()

    async def test_unexpected_end_is_transport_error_and_never_success(self):
        handle = await self.native.processes.spawn(["disconnect"])
        observed = await cancel_before_delivery(handle)
        self.assertIsInstance(observed, sdk.TransportError)
        with self.assertRaises(sdk.TransportError):
            await handle.next_event(timeout=2)
        with self.assertRaises(sdk.TransportError):
            await handle.wait()
        await handle.aclose()

    async def test_binary_stdin_exec_and_pty_capabilities(self):
        manager = self.native.processes
        self.assertEqual(set(manager.capabilities()["supported_signals"]), {9, 15})
        data = b"\x00\xff\x80input"
        result = await self.native.connection.exec(["stdin-program"], stdin=data, timeout=3)
        self.assertEqual((result.stdout, result.exit_code), (data, 7))
        plain = await manager.spawn(["stdin-program"], stdin=True)
        await manager.get(plain.process).send_input(data)
        await manager.get(plain.process).close_stdin()
        plain_output, plain_outcome = await collect(plain)
        self.assertEqual(b"".join(data for _, data in plain_output), data)
        self.assertEqual(plain_outcome.exit.code, 7)
        self.assertIsNone(plain_outcome.native_result)
        handle = await manager.spawn(["pty-program"], stdin=True, pty=(93, 27))
        first = await handle.next_event(timeout=2)
        self.assertEqual((first.channel, first.data), ("terminal", b"\x00\xffterminal"))
        listed = next(info.process for info in await manager.list() if info.process.id == handle.process.id)
        self.assertIsNone(listed.pty)
        before = len(self.records)
        unknown = manager.get(listed)
        for operation in (lambda: unknown.resize(80, 24),
                          lambda: unknown.send_input(b"input"), unknown.close_stdin):
            with self.assertRaises(sdk.UnsupportedError):
                await operation()
        self.assertEqual(len(self.records), before)
        await manager.get(handle.process).resize(121, 43)
        with self.assertRaises(sdk.UnsupportedError):
            await manager.get(handle.process).signal(2)
        await manager.get(handle.process).send_input(data + b"\x04")
        output, outcome = await collect(handle)
        self.assertEqual(b"".join(data for _, data in output), data + b"\x04")
        self.assertEqual(outcome.exit.code, 7)
        self.assertEqual((await handle.wait()).exit.code, 7)

    async def test_cancel_after_native_consumption_before_python_delivery_retains_event(self):
        manager = self.native.processes
        handle = await manager.spawn(["pty-program"], stdin=True, pty=(80, 24))
        observed = await cancel_before_delivery(handle)
        self.assertEqual(observed.data, b"\x00\xffterminal")
        event = await handle.next_event(timeout=2)
        self.assertEqual((event.channel, event.data), ("terminal", b"\x00\xffterminal"))
        await manager.get(handle.process).send_input(b"second\x04")
        output = []
        while True:
            observed = await cancel_before_delivery(handle)
            event = await handle.next_event(timeout=2)
            if observed.outcome is not None:
                self.assertEqual((observed.outcome.exit.code, event.outcome.exit.code), (7, 7))
                break
            self.assertEqual((event.channel, event.data), (observed.channel, observed.data))
            output.append(event.data)
        self.assertEqual(b"".join(output), b"second\x04")
        self.assertEqual((await handle.wait()).exit.code, 7)
        self.assertIsNone(await handle.next_event())


class AsyncDockerTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_explicit_delete_and_shared_dto(self):
        backend = aio.docker(os.environ["OPENSOL_TEST_IMAGE"], command=["sleep", "infinity"])
        lifecycle = backend.lifecycle
        self.assertFalse(lifecycle.capabilities()["stop_preserves_memory"])
        resource = await lifecycle.create()
        self.assertIsInstance(resource, sdk.ResourceRef)
        try:
            self.assertEqual((await lifecycle.inspect(resource)).state, "stopped")
            await lifecycle.start(resource)
            result = await backend.connection(resource).exec(
                ["python3", "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                stdin=b"\x00\xffdocker", timeout=5,
            )
            self.assertEqual(result.stdout, b"\x00\xffdocker")
            self.assertTrue((await aio.docker_connection(resource.id).exec(["true"])).success)
            with self.assertRaises(sdk.UnsupportedError):
                await lifecycle.fork(resource)
            await lifecycle.stop(resource)
            self.assertEqual((await lifecycle.inspect(resource)).state, "stopped")
        finally:
            await lifecycle.delete(resource)
        with self.assertRaises(sdk.NotFoundError):
            await lifecycle.inspect(resource)


class AsyncProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_e2b_lifecycle_and_readonly_envd_discovery(self):
        records = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_request(self):
                assert self.headers["x-api-key"] == "secret"
                records.append((self.command, self.path))
                self.rfile.read(int(self.headers.get("content-length", 0)))
                self.send_response(200)
                self.send_header("content-type", "application/json")
                value = [{"sandbox": {"sandboxID": "child"}}] if self.path.endswith("/fork") else {
                    "sandboxID": "parent", "state": "running", "envdAccessToken": "envd-secret",
                }
                data = json.dumps(value).encode()
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = handle_request

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        try:
            backend = aio.e2b("secret", api_endpoint=endpoint, envd_endpoint=endpoint)
            lifecycle = backend.lifecycle
            self.assertTrue(lifecycle.capabilities()["stop_preserves_memory"])
            creation = lifecycle.create()
            self.addCleanup(creation.close)
            await asyncio.sleep(.03)
            self.assertEqual(records, [], "an unscheduled create must not send a request")
            cancelled = asyncio.create_task(lifecycle.create())
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            self.assertEqual(records, [])
            resource = await asyncio.create_task(creation)
            self.assertEqual((await lifecycle.inspect(resource)).state, "running")
            await lifecycle.stop(resource)
            await lifecycle.start(resource)
            self.assertEqual((await lifecycle.fork(resource)).id, "child")
            before = len(records)
            native = await backend.envd(resource)
            self.assertIsInstance(native.connection, aio.Connection)
            self.assertIsInstance(native.processes, aio.ProcessManager)
            self.assertEqual(records[before:], [("GET", "/sandboxes/parent")])
            await lifecycle.delete(resource)
        finally:
            server.shutdown()
            worker.join(2)
            server.server_close()

    async def test_network_factories_support_coroutine_task_scheduling(self):
        # A real HTTP/2 gRPC server completes each native connection handshake.
        # Provider RPC payload/TLS contracts are covered by the Rust schema suite.
        server = grpc.server(ThreadPoolExecutor(max_workers=1))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        endpoint = f"http://127.0.0.1:{port}"
        try:
            sandboxd = await asyncio.create_task(aio.sandboxd(endpoint))
            backend = await asyncio.create_task(aio.openshell(endpoint, workload_template="base"))
            lifecycle = backend.lifecycle
            connection = await asyncio.create_task(aio.openshell_connection(endpoint, "sandbox"))
            self.assertIsInstance(sandboxd, aio.Connection)
            self.assertIsInstance(lifecycle, aio.Lifecycle)
            self.assertIsInstance(connection, aio.Connection)
            self.assertIsInstance(backend.connection(sdk.ResourceRef("openshell", "sandbox")), aio.Connection)
            with self.assertRaises(sdk.InvalidArgumentError):
                backend.connection(sdk.ResourceRef("docker", "sandbox"))
            synchronous = sdk.openshell(endpoint, workload_template="base")
            self.assertIsInstance(synchronous.lifecycle, sdk.Lifecycle)
            self.assertIsInstance(synchronous.connection(sdk.ResourceRef("openshell", "sandbox")), sdk.Connection)
            self.assertFalse(lifecycle.capabilities()["stop_preserves_memory"])
            with self.assertRaises(sdk.UnsupportedError):
                await lifecycle.fork(sdk.ResourceRef("openshell", "sandbox"))
            with self.assertRaises(sdk.UnsupportedError):
                await sandboxd.exec(["unimplemented"])
        finally:
            server.stop(0).wait(2)


if __name__ == "__main__":
    unittest.main()
