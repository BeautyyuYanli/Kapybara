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
        def __getattr__(self, name):
            return getattr(native, name)

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
    def test_public_io_methods_are_coroutines_and_constructors_are_local(self):
        # Coroutine functions are the public scheduling contract, including methods
        # that can reject unsupported operations when their coroutine is executed.
        methods = {
            sdk.AsyncConnection: ("exec", "shell"),
            sdk.AsyncSandboxManager: ("create", "list"),
            sdk.AsyncSandbox: ("inspect", "start", "stop", "delete", "fork"),
            sdk.AsyncProcessManager: ("probe", "list", "spawn"),
            sdk.AsyncProcess: ("inspect", "wait", "pause", "start", "restart", "kill", "remove",
                          "signal", "send_input", "close_stdin", "resize"),
            sdk.AsyncOutput: ("read", "tail", "subscribe"),
            sdk.AsyncTerminal: ("read", "tail", "screen"),
            sdk.AsyncOutputSubscription: ("next_event", "aclose", "__anext__"),
        }
        for owner, names in methods.items():
            for name in names:
                with self.subTest(owner=owner.__name__, name=name):
                    self.assertTrue(inspect.iscoroutinefunction(getattr(owner, name)))
        for owner in (sdk.AsyncBackend, sdk.AsyncConnection, sdk.AsyncSandboxManager, sdk.AsyncProcessManager):
            self.assertFalse(inspect.iscoroutinefunction(owner.__init__))
        for config in (sdk.LocalConfig, sdk.DockerConfig, sdk.E2bConfig, sdk.EnvdConfig,
                       sdk.AgentSandboxConfig, sdk.OpenShellConfig, sdk.SandboxdConfig,
                       sdk.PueueConfig, sdk.TmuxConfig):
            self.assertFalse(inspect.iscoroutinefunction(config))
        self.assertFalse(inspect.iscoroutinefunction(sdk.AsyncProcess.capabilities))

    def test_command_coroutine_can_be_created_before_the_loop(self):
        connection = sdk.AsyncConnection(sdk.LocalConfig())
        command = connection.exec(["printf", "%s", "scheduled"])

        async def run():
            return await asyncio.create_task(command)

        self.assertEqual(asyncio.run(run()).stdout, b"scheduled")


class AsyncConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_wait_for_scheduling_and_cancel_before_start_has_no_effect(self):
        connection = sdk.AsyncConnection(sdk.LocalConfig())
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
        connection = sdk.AsyncConnection(sdk.LocalConfig())
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

    async def asyncSetUp(self):
        test_sdk.PueueTests.setUp(self)
        self.manager = sdk.AsyncProcessManager(sdk.PueueConfig(config_file=str(self.config)),
                                                    connection=sdk.AsyncConnection(sdk.LocalConfig()))

    async def running(self, process):
        for _ in range(100):
            if (await self.manager.get(process).inspect()).state == "running":
                return
            await asyncio.sleep(0.03)
        self.fail("native Pueue task did not start")

    async def test_cancelled_reads_timeouts_and_input_keep_task_and_subscription(self):
        manager = self.manager
        self.assertEqual(set(manager.capabilities()["supported_signals"]), {2, 9, 15})
        process = await manager.spawn(
            ["/bin/sh", "-c", "read -r value; printf '%s' \"$value\"; exit 7"], stdin=True,
        )
        handle = await process.output.subscribe()
        await self.running(process.ref)
        reading = asyncio.create_task(handle.next_event())
        await asyncio.sleep(0.03)
        reading.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reading
        with self.assertRaises(sdk.TimeoutError):
            await handle.next_event(timeout=0.02)
        with self.assertRaises(sdk.TimeoutError):
            await process.wait(timeout=0.02)
        self.assertEqual((await process.inspect()).state, "running")
        with self.assertRaises(sdk.UnsupportedError):
            await process.close_stdin()
        with self.assertRaises(sdk.UnsupportedError):
            await process.resize(80, 24)
        restored = sdk.ProcessRef("pueue", process.ref.id)
        await manager.get(restored).send_input(b"hello '$()\n")
        output, outcome = await collect(handle)
        self.assertTrue(all(channel == "combined" for channel, _ in output))
        self.assertEqual(b"".join(data for _, data in output), b"hello '$()")
        self.assertIsInstance(outcome, sdk.ProcessOutcome)
        self.assertEqual((outcome.exit.code, outcome.native_result), (7, "Failed"))
        self.assertIsNone(await handle.next_event())
        self.assertEqual((await process.wait(timeout=2)).exit.code, 7)
        restored = sdk.ProcessRef(process.ref.backend, process.ref.id)
        replay = await manager.get(restored).output.subscribe()
        replay_output, replay_outcome = await collect(replay)
        self.assertEqual(b"".join(data for _, data in replay_output), b"hello '$()")
        self.assertEqual(replay_outcome.exit.code, 7)
        await replay.aclose()

    async def test_unconsumed_subscriptions_reject_new_execution_and_leave_completed_subscriptions_ended(self):
        script = self.directory / "command.sh"
        script.write_text("printf 7; exit 7")
        process = await self.manager.spawn(["sh", str(script)])
        initial = await process.output.subscribe()

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
        self.assertIsNone(await cached.next_event())
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

    async def test_cancel_before_delivery_preserves_native_only_completion(self):
        process = await self.manager.spawn(["sleep", "30"])
        handle = await process.output.subscribe()
        await self.running(process.ref)
        await process.signal(9)
        # Isolate delivery cancellation from the native CLI's two-second wait polling.
        await process.wait(timeout=5)
        observed = await cancel_before_delivery(handle)
        self.assertIsNone(observed.outcome.exit)
        self.assertEqual(observed.outcome.native_result, "Killed")
        event = await handle.next_event(timeout=2)
        self.assertIsNone(event.outcome.exit)
        self.assertEqual(event.outcome.native_result, "Killed")
        self.assertIsNone(event.channel)
        self.assertEqual(event.data, b"")
        self.assertIsNone(await handle.next_event())
        await handle.aclose()

    async def test_aclose_wakes_reader_without_signalling_and_is_idempotent(self):
        process = await self.manager.spawn(["sleep", "10"])
        handle = await process.output.subscribe()
        await self.running(process.ref)
        reading = asyncio.create_task(handle.next_event())
        await asyncio.sleep(0.03)
        await handle.aclose()
        self.assertIsNone(await asyncio.wait_for(reading, 2))
        await handle.aclose()
        self.assertEqual((await process.inspect()).state, "running")
        self.assertIsNone(await handle.next_event())
        await process.signal(9)
        outcome = await process.wait(timeout=3)
        self.assertIsNone(outcome.exit)
        self.assertEqual(outcome.native_result, "Killed")
        self.assertIsNone((await process.inspect()).outcome.exit)


class AsyncEnvdTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        test_sdk.EnvdBindingTests.setUp(self)
        self.backend = sdk.AsyncBackend(sdk.EnvdConfig(self.endpoint))
        self.processes = sdk.AsyncProcessManager(self.backend)
        self.connection = sdk.AsyncConnection(self.backend)

    async def test_views_and_unscheduled_io_preserve_native_initial_stream(self):
        process = await self.processes.spawn(["stdin-program"], stdin=True)
        output, terminal = process.output, process.terminal
        self.assertIsInstance(output, sdk.AsyncOutput)
        self.assertIsInstance(terminal, sdk.AsyncTerminal)
        self.assertTrue(output.capabilities()["streaming_output"])
        self.assertFalse(terminal.capabilities()["read"])
        for coroutine in (output.subscribe(), output.read(), output.tail(), terminal.read(),
                          terminal.tail(), terminal.screen()):
            self.assertTrue(inspect.iscoroutine(coroutine))
            task = asyncio.create_task(coroutine)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual([method for method, _ in self.records], ["Start"])
        initial = await process.output.subscribe()
        self.assertFalse(hasattr(initial, "ref"))
        await process.close_stdin()
        self.assertEqual(((await collect(initial))[1]).exit.code, 7)

    async def test_initial_stream_and_additional_subscription_use_start_and_connect(self):
        manager = self.processes
        process = await manager.spawn(["pty-program"], stdin=True, pty=(80, 24))
        initial = await process.output.subscribe()

        self.assertEqual((await initial.next_event(timeout=2)).data, b"\x00\xffterminal")
        self.assertEqual([method for method, _ in self.records], ["Start"])
        restored = sdk.ProcessRef("envd", process.ref.id, pty=True)
        offline = (sdk.AsyncProcessManager(sdk.EnvdConfig("http://127.0.0.1:1"))).get(restored)
        self.assertIsInstance(offline, sdk.AsyncProcess)
        replay = await manager.get(restored).output.subscribe()
        self.assertFalse(hasattr(replay, "ref"))
        self.assertEqual((await replay.next_event(timeout=2)).data, b"\x00\xffterminal")
        await process.send_input(b"\x00\xffreconnected\x04")
        original_output, original_outcome = await collect(initial)
        replay_output, replay_outcome = await collect(replay)
        self.assertEqual(original_output, replay_output)
        self.assertTrue(all(channel == "terminal" for channel, _ in replay_output))
        self.assertEqual(b"".join(data for _, data in replay_output), b"\x00\xffreconnected\x04")
        self.assertEqual((original_outcome.exit.code, replay_outcome.exit.code), (7, 7))
        connected = [message for method, message in self.records if method == "Connect"]
        self.assertEqual([message.process.pid for message in connected], [int(restored.id)])
        await initial.aclose()
        await replay.aclose()

    async def test_unexpected_end_is_transport_error_and_never_success(self):
        process = await self.processes.spawn(["disconnect"])
        handle = await process.output.subscribe()
        observed = await cancel_before_delivery(handle)
        self.assertIsInstance(observed, sdk.TransportError)
        with self.assertRaises(sdk.TransportError):
            await handle.next_event(timeout=2)
        self.assertIsNone(await handle.next_event())
        await handle.aclose()



    async def test_binary_stdin_exec_and_pty_capabilities(self):
        manager = self.processes
        self.assertEqual(set(manager.capabilities()["supported_signals"]), {9, 15})
        data = b"\x00\xff\x80input"
        result = await self.connection.exec(["stdin-program"], stdin=data, timeout=3)
        self.assertEqual((result.stdout, result.exit_code), (data, 7))
        plain_process = await manager.spawn(["stdin-program"], stdin=True)
        self.assertEqual(manager.get(plain_process.ref).capabilities(), plain_process.capabilities())
        self.assertTrue(plain_process.capabilities()["stdin"])
        self.assertTrue(plain_process.capabilities()["close_stdin"])
        self.assertFalse(plain_process.capabilities()["pty"])
        self.assertFalse(plain_process.capabilities()["resize"])
        plain = await plain_process.output.subscribe()
        await plain_process.send_input(data)
        await plain_process.close_stdin()
        plain_output, plain_outcome = await collect(plain)
        self.assertEqual(b"".join(data for _, data in plain_output), data)
        self.assertEqual(plain_outcome.exit.code, 7)
        self.assertIsNone(plain_outcome.native_result)
        process = await manager.spawn(["pty-program"], stdin=True, pty=(93, 27))
        handle = await process.output.subscribe()
        first = await handle.next_event(timeout=2)
        self.assertEqual((first.channel, first.data), ("terminal", b"\x00\xffterminal"))
        listed = next(info.process for info in await manager.list() if info.process.id == process.ref.id)
        self.assertIsNone(listed.pty)
        before = len(self.records)
        unknown = manager.get(listed)
        for capability in ("stdin", "binary_stdin", "close_stdin", "pty", "resize"):
            self.assertFalse(unknown.capabilities()[capability])
        self.assertTrue(process.capabilities()["stdin"])
        self.assertTrue(process.capabilities()["resize"])
        self.assertFalse(process.capabilities()["close_stdin"])
        for operation in (lambda: unknown.resize(80, 24),
                          lambda: unknown.send_input(b"input"), unknown.close_stdin):
            with self.assertRaises(sdk.UnsupportedError):
                await operation()
        self.assertEqual(len(self.records), before)
        await process.resize(121, 43)
        with self.assertRaises(sdk.UnsupportedError):
            await process.signal(2)
        await process.send_input(data + b"\x04")
        output, outcome = await collect(handle)
        self.assertEqual(b"".join(data for _, data in output), data + b"\x04")
        self.assertEqual(outcome.exit.code, 7)

    async def test_cancel_after_native_consumption_before_python_delivery_retains_event(self):
        manager = self.processes
        process = await manager.spawn(["pty-program"], stdin=True, pty=(80, 24))
        handle = await process.output.subscribe()
        observed = await cancel_before_delivery(handle)
        self.assertEqual(observed.data, b"\x00\xffterminal")
        event = await handle.next_event(timeout=2)
        self.assertEqual((event.channel, event.data), ("terminal", b"\x00\xffterminal"))
        await process.send_input(b"second\x04")
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
        self.assertIsNone(await handle.next_event())


class AsyncDockerTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_explicit_delete_and_shared_dto(self):
        backend = sdk.AsyncBackend(sdk.DockerConfig())
        lifecycle = sdk.AsyncSandboxManager(backend)
        self.assertFalse(lifecycle.capabilities()["stop_preserves_memory"])
        resource = await lifecycle.create(sdk.DockerSandboxSpec(os.environ["OPENSOL_TEST_IMAGE"],
                                              command=["sleep", "infinity"], labels={"project": "async-python"}))
        self.assertIsInstance(resource, sdk.AsyncSandbox)
        try:
            self.assertTrue(lifecycle.capabilities()["list"])
            listed = next(info for info in await lifecycle.list() if info.sandbox.id == resource.ref.id)
            self.assertIsInstance(listed, sdk.SandboxInfo)
            self.assertEqual(listed.state, "stopped")
            self.assertTrue(listed.name)
            self.assertFalse(listed.name.startswith("/"))
            self.assertEqual(listed.labels["project"], "async-python")
            inspected = await resource.inspect()
            self.assertEqual((inspected.name, inspected.labels), (listed.name, listed.labels))
            rebound = lifecycle.get(listed.sandbox)
            self.assertIsInstance(rebound, sdk.AsyncSandbox)
            self.assertEqual((await rebound.inspect()).state, "stopped")
            self.assertEqual((await resource.inspect()).state, "stopped")
            await resource.start()
            connection = sdk.AsyncConnection(backend, target=resource.ref)
            result = await connection.exec(
                ["python3", "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                stdin=b"\x00\xffdocker", timeout=5,
            )
            self.assertEqual(result.stdout, b"\x00\xffdocker")
            direct = sdk.AsyncConnection(sdk.DockerConfig(), target=resource.ref)
            self.assertTrue((await direct.exec(["true"])).success)
            with self.assertRaises(sdk.UnsupportedError):
                await resource.fork()
            await resource.stop()
            self.assertEqual((await resource.inspect()).state, "stopped")
        finally:
            await resource.delete()
        with self.assertRaises(sdk.NotFoundError):
            await resource.inspect()


class AsyncProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_e2b_lifecycle_and_readonly_envd_discovery(self):
        records = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_request(self):
                records.append((self.command, self.path))
                self.rfile.read(int(self.headers.get("content-length", 0)))
                if self.path == "/process.Process/List":
                    assert self.headers["x-access-token"] == "envd-secret"
                    self.send_response(200)
                    self.send_header("content-type", "application/proto")
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                assert self.headers["x-api-key"] == "secret"
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
            backend = sdk.AsyncBackend(sdk.E2bConfig("secret", api_endpoint=endpoint, envd_endpoint=endpoint))
            lifecycle = sdk.AsyncSandboxManager(backend)
            self.assertTrue(lifecycle.capabilities()["stop_preserves_memory"])
            creation = lifecycle.create(sdk.E2bSandboxSpec())
            self.addCleanup(creation.close)
            await asyncio.sleep(.03)
            self.assertEqual(records, [], "an unscheduled create must not send a request")
            cancelled = asyncio.create_task(lifecycle.create(sdk.E2bSandboxSpec()))
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            self.assertEqual(records, [])
            resource = await asyncio.create_task(creation)
            self.assertEqual((await resource.inspect()).state, "running")
            await resource.stop()
            await resource.start()
            self.assertEqual((await resource.fork()).ref.id, "child")
            before = len(records)
            connection = sdk.AsyncConnection(backend, target=resource.ref)
            processes = sdk.AsyncProcessManager(backend, target=resource.ref)
            self.assertIsInstance(connection, sdk.AsyncConnection)
            self.assertIsInstance(processes, sdk.AsyncProcessManager)
            self.assertEqual(records[before:], [], "binding and conversion are local")
            self.assertEqual(await processes.list(), [])
            self.assertEqual(processes.to_process_manager().list(), [])
            self.assertEqual(records[before:], [("GET", "/sandboxes/parent"),
                                                ("POST", "/process.Process/List"),
                                                ("POST", "/process.Process/List")])
            await resource.delete()
        finally:
            server.shutdown()
            worker.join(2)
            server.server_close()

    async def test_grpc_io_supports_coroutine_task_scheduling(self):
        # A real HTTP/2 gRPC server completes handshakes on the first native RPC.
        # Provider RPC payload/TLS contracts are covered by the Rust schema suite.
        server = grpc.server(ThreadPoolExecutor(max_workers=1))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        endpoint = f"http://127.0.0.1:{port}"
        try:
            sandboxd = sdk.AsyncConnection(sdk.SandboxdConfig(endpoint))
            backend = sdk.AsyncBackend(sdk.OpenShellConfig(endpoint))
            lifecycle = sdk.AsyncSandboxManager(backend)
            connection = sdk.AsyncConnection(sdk.OpenShellConfig(endpoint), target=sdk.SandboxRef("openshell", "sandbox"))
            self.assertIsInstance(sandboxd, sdk.AsyncConnection)
            self.assertIsInstance(lifecycle, sdk.AsyncSandboxManager)
            self.assertIsInstance(connection, sdk.AsyncConnection)
            self.assertIsInstance(sdk.AsyncConnection(backend, target=sdk.SandboxRef("openshell", "sandbox")), sdk.AsyncConnection)
            with self.assertRaises(sdk.InvalidArgumentError):
                sdk.AsyncConnection(backend, target=sdk.SandboxRef("docker", "sandbox"))
            synchronous = sdk.Backend(sdk.OpenShellConfig(endpoint))
            self.assertIsInstance(sdk.SandboxManager(synchronous), sdk.SandboxManager)
            self.assertIsInstance(sdk.Connection(synchronous, target=sdk.SandboxRef("openshell", "sandbox")), sdk.Connection)
            self.assertFalse(lifecycle.capabilities()["stop_preserves_memory"])
            with self.assertRaises(sdk.UnsupportedError):
                await lifecycle.get(sdk.SandboxRef("openshell", "sandbox")).fork()
            with self.assertRaises(sdk.UnsupportedError):
                await asyncio.create_task(sandboxd.exec(["unimplemented"]))
        finally:
            server.stop(0).wait(2)


if __name__ == "__main__":
    unittest.main()
