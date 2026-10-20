"""Exercise the installed native wheel with real processes and Docker inside the test container."""

import gc
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import opensol as sdk
import process_pb2 as envd_protocol


def collect(handle):
    output = []
    while (event := handle.next_event(timeout=5)) is not None:
        if event.outcome is not None:
            return output, event.outcome
        output.append((event.channel, event.data))
    raise AssertionError("native stream ended without completion")


def combined_output(output):
    if any(channel != "combined" for channel, _data in output):
        raise AssertionError("Pueue must label every output chunk as combined")
    return b"".join(data for _channel, data in output)


class ConnectionTests(unittest.TestCase):
    def test_argv_bytes_environment_working_directory_and_nonzero_exit(self):
        connection = sdk.local()
        with tempfile.TemporaryDirectory(prefix="sdk ' cwd ") as directory:
            value = "x '$()\n你好"
            data = b"\x00\xff\x80input" * 20_000
            result = connection.exec(
                [
                    "python3",
                    "-c",
                    "import os,sys; assert os.getcwd()==sys.argv[1]; "
                    "assert sys.argv[2]==os.environ['SDK_TEST']; "
                    "sys.stdout.buffer.write(sys.stdin.buffer.read()); "
                    "sys.stderr.buffer.write(b'\\xfferror'); sys.exit(7)",
                    directory,
                    value,
                ],
                cwd=directory,
                env={"SDK_TEST": value},
                stdin=data,
                timeout=5,
            )
        self.assertIsInstance(result, sdk.ExecResult)
        self.assertEqual(result.stdout, data)
        self.assertEqual(result.stderr, b"\xfferror")
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.exit.code, 7)
        self.assertFalse(result.success)
        self.assertEqual(connection.shell("printf '%s' shell").stdout, b"shell")
        self.assertTrue(connection.capabilities()["stdin"])

    def test_errors_timeout_and_signal_are_distinct(self):
        connection = sdk.local()
        with self.assertRaises(sdk.NotFoundError) as caught:
            connection.exec(["/no-such-opensol-program"])
        self.assertEqual(caught.exception.backend, "local")
        self.assertEqual(caught.exception.operation, "exec")
        self.assertEqual(caught.exception.kind, "NotFound")
        with self.assertRaises(sdk.TimeoutError):
            connection.exec(["sleep", "2"], timeout=0.05)
        result = connection.shell("kill -TERM $$")
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.signal, 15)
        self.assertFalse(result.success)
        for argv in ([], [""], ["printf", "\x00"]):
            with self.subTest(argv=argv), self.assertRaises(sdk.InvalidArgumentError):
                connection.exec(argv)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(sdk.InvalidArgumentError):
                connection.exec(["true"], timeout=timeout)

    def test_blocking_io_releases_the_gil(self):
        started = threading.Event()
        finished = threading.Event()
        failures = []

        def execute():
            started.set()
            try:
                sdk.local().exec(["sleep", "0.6"])
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=execute)
        worker.start()
        self.assertTrue(started.wait(1))
        time.sleep(0.05)
        self.assertFalse(finished.is_set(), "Python must run while Rust I/O is blocked")
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])


class PueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="opensol-pueue-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = self.directory / "pueue.yml"
        self.config.write_text(
            f"shared:\n  pueue_directory: {json.dumps(directory.name)}\n"
            f"  runtime_directory: {json.dumps(directory.name)}\n"
            "client:\n  read_local_logs: true\n"
        )
        daemon = subprocess.Popen(
            ["pueued", "--config", str(self.config)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(self.stop_daemon, daemon)
        for _attempt in range(100):
            result = subprocess.run(
                ["pueue", "--config", str(self.config), "status", "--json"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if result.returncode == 0:
                break
            self.assertIsNone(daemon.poll(), "pueued exited before becoming ready")
            time.sleep(0.03)
        else:
            self.fail("pueued did not become ready")
        self.manager = sdk.pueue(sdk.local(), config_file=str(self.config))

    def stop_daemon(self, daemon):
        subprocess.run(
            ["pueue", "--config", str(self.config), "kill", "--all"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if daemon.poll() is None:
            os.killpg(daemon.pid, signal.SIGKILL)
        daemon.wait(timeout=5)

    def running(self, process):
        for _attempt in range(100):
            if self.manager.get(process).inspect().state == "running":
                return
            time.sleep(0.03)
        self.fail("native task did not start")

    def test_task_output_identity_and_reconnect(self):
        process, handle = self.manager.spawn(
            [
                "python3",
                "-c",
                "import os,sys; assert os.environ['SDK_TEST']==sys.argv[1]; "
                "sys.stdout.buffer.write(b'\\x00\\xffout'); sys.stdout.buffer.flush(); "
                "sys.stderr.buffer.write(b'err'); sys.exit(7)",
                "x '$()\n你好",
            ],
            cwd=str(self.directory),
            env={"SDK_TEST": "x '$()\n你好"},
        )
        self.assertIsInstance(process, sdk.Process)
        self.assertIsInstance(handle, sdk.OutputSubscription)
        reference = handle.ref
        self.assertIsInstance(reference, sdk.ProcessRef)
        self.assertEqual(process.ref.id, reference.id)
        output, outcome = collect(handle)
        self.assertEqual(combined_output(output), b"\x00\xffouterr")
        self.assertIsInstance(outcome, sdk.ProcessOutcome)
        self.assertEqual((outcome.exit.code, outcome.native_result), (7, "Failed"))
        self.assertEqual(handle.wait().exit.code, 7)
        restored = sdk.ProcessRef(reference.backend, reference.id)
        self.assertEqual(self.manager.get(restored).inspect().state, "exited")
        self.assertEqual(self.manager.get(restored).wait(timeout=2).exit.code, 7)
        replay, replay_outcome = collect(self.manager.get(restored).output.subscribe())
        self.assertEqual(combined_output(replay), b"\x00\xffouterr")
        self.assertEqual(replay_outcome.exit.code, 7)
        capabilities = process.output.capabilities()
        self.assertTrue(capabilities["historical_output"])
        self.assertFalse(capabilities["streaming_output"])

    def test_stdin_polling_timeouts_and_unsupported_capabilities(self):
        process, handle = self.manager.spawn(
            ["/bin/sh", "-c", "read -r value; printf '%s' \"$value\""],
            stdin=True,
        )
        self.running(handle.ref)
        with self.assertRaises(sdk.TimeoutError):
            handle.next_event(timeout=0.03)
        with self.assertRaises(sdk.TimeoutError):
            process.wait(timeout=0.03)
        self.assertEqual(process.inspect().state, "running")
        with self.assertRaises(sdk.UnsupportedError):
            process.send_input(b"\xff")
        with self.assertRaises(sdk.UnsupportedError):
            process.send_input(b"\x00")
        with self.assertRaises(sdk.UnsupportedError):
            process.close_stdin()
        with self.assertRaises(sdk.UnsupportedError):
            self.manager.spawn(["true"], pty=(80, 24))
        restored = sdk.ProcessRef("pueue", handle.ref.id)
        self.manager.get(restored).send_input(b"hello '$()\n")
        output, outcome = collect(handle)
        self.assertEqual(combined_output(output), b"hello '$()")
        self.assertTrue(outcome.exit.success)

    def test_close_wakes_blocked_reader_without_killing_task(self):
        process, handle = self.manager.spawn(["sleep", "5"])
        self.running(handle.ref)
        started = threading.Event()
        results = []

        def read():
            started.set()
            try:
                results.append(handle.next_event())
            except BaseException as error:
                results.append(error)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        self.assertTrue(started.wait(1))
        time.sleep(0.03)
        handle.close()
        reader.join(2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(results, [None])
        self.assertEqual(process.inspect().state, "running")
        with self.assertRaises(sdk.TransportError):
            handle.wait()
        process.signal(9)
        outcome = process.wait(timeout=3)
        self.assertIsNone(outcome.exit)
        self.assertEqual(outcome.native_result, "Killed")
        self.assertIsNone(process.inspect().outcome.exit)


class EnvdBindingTests(unittest.TestCase):
    def setUp(self):
        records = self.records = []
        processes = {}
        failures = []
        lock = threading.Lock()

        def event_frame(event, response_type):
            payload = response_type(event=event).SerializeToString()
            return b"\x00" + len(payload).to_bytes(4, "big") + payload

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def reply(self, status, content_type, data):
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                try:
                    self.handle_rpc()
                except Exception as error:
                    failures.append(error)
                    self.reply(500, "application/json", b'{"code":"internal"}')

            def handle_rpc(self):
                assert self.headers["connect-protocol-version"] == "1"
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                method = self.path.rsplit("/", 1)[1]
                if method == "List":
                    assert self.headers["content-type"] == "application/proto"
                    message = envd_protocol.ListRequest.FromString(body)
                    with lock:
                        records.append((method, message))
                        response = envd_protocol.ListResponse(processes=[
                            envd_protocol.ProcessInfo(pid=pid)
                            for pid, process in processes.items()
                            if not process["completed"].is_set()
                        ])
                    self.reply(200, "application/proto", response.SerializeToString())
                    return
                if method in ("Start", "Connect"):
                    assert self.headers["content-type"] == "application/connect+proto"
                    assert body[0] == 0 and int.from_bytes(body[1:5], "big") == len(body) - 5
                    if method == "Start":
                        message = envd_protocol.StartRequest.FromString(body[5:])
                        response_type = envd_protocol.StartResponse
                        with lock:
                            pid = 42 + len(processes)
                            process = {
                                "pty": message.HasField("pty"),
                                "streaming": message.process.cmd == "stream-program",
                                "input": bytearray(),
                                "completed": threading.Event(),
                            }
                            processes[pid] = process
                            records.append((method, message))
                    else:
                        message = envd_protocol.ConnectRequest.FromString(body[5:])
                        response_type = envd_protocol.ConnectResponse
                        assert message.process.WhichOneof("selector") == "pid"
                        with lock:
                            pid = message.process.pid
                            process = processes[pid]
                            records.append((method, message))
                    self.send_response(200)
                    self.send_header("content-type", "application/connect+proto")
                    self.send_header("connection", "close")
                    self.end_headers()
                    self.wfile.write(
                        event_frame(
                            envd_protocol.ProcessEvent(
                                start=envd_protocol.ProcessEvent.StartEvent(pid=pid)
                            ), response_type,
                        )
                    )
                    if process["pty"]:
                        self.wfile.write(
                            event_frame(
                                envd_protocol.ProcessEvent(
                                    data=envd_protocol.ProcessEvent.DataEvent(
                                        pty=b"\x00\xffterminal"
                                    )
                                ), response_type,
                            )
                        )
                    self.wfile.flush()
                    if method == "Start" and message.process.cmd == "disconnect":
                        self.close_connection = True
                        return
                    if process["streaming"]:
                        # Real ConnectRPC data frames continue across the client's wait deadline.
                        for _ in range(500):
                            if process["completed"].wait(.01):
                                break
                            self.wfile.write(event_frame(envd_protocol.ProcessEvent(
                                data=envd_protocol.ProcessEvent.DataEvent(stdout=b"tick")), response_type))
                            self.wfile.flush()
                    assert process["completed"].wait(5), "fixture process did not finish"
                    with lock:
                        data = bytes(process["input"])
                    output = {"pty" if process["pty"] else "stdout": data}
                    self.wfile.write(
                        event_frame(
                            envd_protocol.ProcessEvent(
                                data=envd_protocol.ProcessEvent.DataEvent(**output)
                            ), response_type,
                        )
                        + event_frame(
                            envd_protocol.ProcessEvent(
                                end=envd_protocol.ProcessEvent.EndEvent(
                                    exit_code=7, exited=True, status="exit status 7"
                                )
                            ), response_type,
                        )
                        + b"\x02\x00\x00\x00\x02{}"
                    )
                    self.close_connection = True
                    return
                request_types = {
                    "SendInput": (envd_protocol.SendInputRequest, envd_protocol.SendInputResponse),
                    "CloseStdin": (
                        envd_protocol.CloseStdinRequest,
                        envd_protocol.CloseStdinResponse,
                    ),
                    "Update": (envd_protocol.UpdateRequest, envd_protocol.UpdateResponse),
                    "SendSignal": (envd_protocol.SendSignalRequest, envd_protocol.SendSignalResponse),
                }
                if method not in request_types:
                    self.reply(404, "application/json", b'{"code":"unimplemented"}')
                    return
                assert self.headers["content-type"] == "application/proto"
                request_type, response_type = request_types[method]
                message = request_type.FromString(body)
                assert message.process.WhichOneof("selector") == "pid"
                with lock:
                    process = processes[message.process.pid]
                    records.append((method, message))
                    if method == "SendInput":
                        channel = message.input.WhichOneof("input")
                        assert channel == ("pty" if process["pty"] else "stdin")
                        process["input"].extend(getattr(message.input, channel))
                        if process["pty"] and process["input"].endswith(b"\x04"):
                            process["completed"].set()
                    elif method == "CloseStdin":
                        assert not process["pty"]
                        process["completed"].set()
                    elif method == "SendSignal":
                        assert message.signal in (9, 15)
                        process["completed"].set()
                    else:
                        assert process["pty"] and message.HasField("pty")
                self.reply(200, "application/proto", response_type().SerializeToString())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def close():
            with lock:
                for process in processes.values():
                    process["completed"].set()
            server.shutdown()
            worker.join(2)
            server.server_close()
            self.assertEqual(failures, [])

        self.addCleanup(close)
        self.endpoint = f"http://127.0.0.1:{server.server_port}"
        self.native = sdk.envd(self.endpoint)

    def test_unexpected_end_does_not_cache_a_completion(self):
        process, handle = self.native.processes.spawn(["disconnect"])
        with self.assertRaises(sdk.TransportError):
            handle.next_event(timeout=2)
        with self.assertRaises(sdk.TransportError):
            handle.wait()
        handle.close()

    def test_subscription_wait_has_one_deadline_despite_continuous_native_output(self):
        process, handle = self.native.processes.spawn(["stream-program"], stdin=True)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(sdk.InvalidArgumentError):
                handle.wait(timeout=timeout)
        started = time.monotonic()
        with self.assertRaises(sdk.TimeoutError) as caught:
            handle.wait(timeout=.12)
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual((caught.exception.backend, caught.exception.operation), ("envd", "wait"))
        self.assertEqual(process.inspect().state, "running")
        process.close_stdin()
        self.assertEqual(handle.wait(timeout=2).exit.code, 7)
        self.assertEqual(handle.wait(timeout=1).exit.code, 7)
        handle.close()

    def test_initial_stream_and_additional_subscription_use_start_and_connect(self):
        manager = self.native.processes
        process, initial = manager.spawn(["pty-program"], stdin=True, pty=(80, 24))

        self.assertEqual(initial.next_event(timeout=2).data, b"\x00\xffterminal")
        self.assertEqual([method for method, _ in self.records], ["Start"])
        # Binding a saved reference stays local even for an unreachable endpoint.
        restored = sdk.ProcessRef("envd", process.ref.id, pty=True)
        offline = sdk.envd("http://127.0.0.1:1").processes.get(restored)
        self.assertIsInstance(offline, sdk.Process)
        replay = manager.get(restored).output.subscribe()
        self.assertEqual((replay.ref.id, replay.ref.pty), (restored.id, True))
        self.assertEqual(replay.next_event(timeout=2).data, b"\x00\xffterminal")
        process.send_input(b"\x00\xffreconnected\x04")
        original_output, original_outcome = collect(initial)
        replay_output, replay_outcome = collect(replay)
        self.assertEqual(original_output, replay_output)
        self.assertTrue(all(channel == "terminal" for channel, _ in replay_output))
        self.assertEqual(b"".join(data for _, data in replay_output), b"\x00\xffreconnected\x04")
        self.assertEqual((original_outcome.exit.code, replay_outcome.exit.code), (7, 7))
        self.assertEqual(replay.wait().exit.code, 7)
        connected = [message for method, message in self.records if method == "Connect"]
        self.assertEqual([message.process.pid for message in connected], [int(restored.id)])
        initial.close()
        replay.close()

    def test_binary_process_stdin_and_close_stdin(self):
        manager = self.native.processes
        process, handle = manager.spawn(["stdin-program"], stdin=True)
        self.assertFalse(handle.ref.pty)
        data = b"\x00\xff\x80input\n"
        process.send_input(data)
        process.close_stdin()
        output, outcome = collect(handle)
        self.assertTrue(all(channel == "stdout" for channel, _data in output))
        self.assertEqual(b"".join(data for _channel, data in output), data)
        self.assertEqual(outcome.exit.code, 7)
        self.assertIsNone(outcome.native_result)
        start = next(message for method, message in self.records if method == "Start")
        self.assertTrue(start.stdin)
        self.assertFalse(start.HasField("pty"))
        sent = next(message for method, message in self.records if method == "SendInput")
        self.assertEqual(sent.process.pid, int(handle.ref.id))
        self.assertEqual(sent.input.WhichOneof("input"), "stdin")
        self.assertEqual(sent.input.stdin, data)
        closed = next(message for method, message in self.records if method == "CloseStdin")
        self.assertEqual(closed.process.pid, int(handle.ref.id))

    def test_pty_dimensions_terminal_bytes_binary_input_and_resize(self):
        manager = self.native.processes
        process, handle = manager.spawn(["pty-program"], stdin=True, pty=(93, 27))
        self.assertTrue(handle.ref.pty)
        event = handle.next_event(timeout=2)
        self.assertEqual(event.channel, "terminal")
        self.assertEqual(event.data, b"\x00\xffterminal")
        listed = manager.list()[0].process
        self.assertEqual(listed.id, handle.ref.id)
        self.assertIsNone(listed.pty)
        self.assertIsNone(sdk.ProcessRef("envd", listed.id).pty)
        unknown = manager.get(listed)
        self.assertEqual(unknown.inspect().state, "running")
        before = len(self.records)
        for operation in (lambda: unknown.resize(80, 24),
                          lambda: unknown.send_input(b"input"), unknown.close_stdin):
            with self.assertRaises(sdk.UnsupportedError):
                operation()
        self.assertEqual(len(self.records), before, "unknown mode must never guess an RPC channel")
        process.resize(121, 43)
        data = b"\x00\xff\x80pty\x04"
        process.send_input(data)
        output, outcome = collect(handle)
        self.assertTrue(all(channel == "terminal" for channel, _data in output))
        self.assertEqual(b"".join(data for _channel, data in output), data)
        self.assertEqual(outcome.exit.code, 7)
        start = next(message for method, message in self.records if method == "Start")
        self.assertEqual((start.pty.size.cols, start.pty.size.rows), (93, 27))
        sent = next(message for method, message in self.records if method == "SendInput")
        self.assertEqual(sent.process.pid, int(handle.ref.id))
        self.assertEqual(sent.input.WhichOneof("input"), "pty")
        self.assertEqual(sent.input.pty, data)
        update = next(message for method, message in self.records if method == "Update")
        self.assertEqual(update.process.pid, int(handle.ref.id))
        self.assertEqual((update.pty.size.cols, update.pty.size.rows), (121, 43))


class DockerTests(unittest.TestCase):
    def test_lifecycle_explicit_cleanup_and_binary_exec(self):
        image = os.environ["OPENSOL_TEST_IMAGE"]
        backend = sdk.docker(image, command=["sleep", "infinity"])
        creator = backend.lifecycle
        resource = creator.create()
        admin = sdk.docker(image).lifecycle
        try:
            self.assertEqual(creator.inspect(resource).state, "stopped")
            connection = backend.connection(resource)
            self.assertEqual(creator.inspect(resource).state, "stopped", "binding must not start resources")
            creator.start(resource)
            del creator, backend
            gc.collect()
            self.assertEqual(admin.inspect(resource).state, "running")
            result = connection.exec(
                [
                    "python3",
                    "-c",
                    "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.exit(7)",
                ],
                stdin=b"\x00\xffDocker",
                timeout=5,
            )
            self.assertEqual(result.stdout, b"\x00\xffDocker")
            self.assertEqual(result.exit_code, 7)
            self.assertTrue(sdk.docker_connection(resource.id).exec(["true"]).success)
            with self.assertRaises(sdk.UnsupportedError):
                admin.fork(resource)
            admin.stop(resource)
            self.assertEqual(admin.inspect(resource).state, "stopped")
            admin.start(resource)
            self.assertEqual(admin.inspect(resource).state, "running")
        finally:
            admin.delete(resource)
        with self.assertRaises(sdk.NotFoundError):
            admin.inspect(resource)


class LifecycleBindingTests(unittest.TestCase):
    def test_native_e2b_http_fixture_resource_and_error_conversion(self):
        records = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                records.append((self.command, self.path, self.headers.get("x-api-key"), body))
                sandbox = {
                    "sandboxID": "parent",
                    "state": "running",
                    "envdAccessToken": "envd-secret",
                }
                status = 200
                if self.path.endswith("/missing"):
                    status, value = 404, {"message": "api-secret"}
                elif self.path.endswith("/fork"):
                    status, value = 201, [{"sandbox": {"sandboxID": "child"}}]
                elif self.command == "DELETE" or self.path.endswith("/pause"):
                    status, value = 204, None
                else:
                    status, value = (201 if self.command == "POST" else 200), sandbox
                encoded = b"" if value is None else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = handle_request
            do_POST = handle_request
            do_DELETE = handle_request

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        try:
            backend = sdk.e2b("api-secret", api_endpoint=endpoint, envd_endpoint=endpoint)
            lifecycle = backend.lifecycle
            resource = lifecycle.create(name="label", labels={"owner": "test"})
            self.assertIsInstance(resource, sdk.ResourceRef)
            self.assertEqual((resource.backend, resource.id), ("e2b", "parent"))
            self.assertEqual(lifecycle.inspect(resource).state, "running")
            lifecycle.stop(resource)
            lifecycle.start(resource)
            self.assertEqual(lifecycle.fork(resource).id, "child")
            before = len(records)
            native = backend.envd(resource)
            self.assertEqual([(method, path) for method, path, *_ in records[before:]],
                             [("GET", "/sandboxes/parent")])
            self.assertIsInstance(native, sdk.Envd)
            self.assertIsInstance(native.connection, sdk.Connection)
            self.assertIsInstance(native.processes, sdk.ProcessManager)
            lifecycle.delete(resource)
            with self.assertRaises(sdk.NotFoundError) as caught:
                lifecycle.inspect(sdk.ResourceRef("e2b", "missing"))
            self.assertEqual(caught.exception.backend, "e2b")
            self.assertEqual(caught.exception.operation, "inspect")
            self.assertNotIn("api-secret", str(caught.exception))
        finally:
            server.shutdown()
            worker.join(2)
            server.server_close()
        self.assertTrue(all(record[2] == "api-secret" for record in records))
        create = next(record for record in records if record[1] == "/v2/sandboxes")
        self.assertEqual(json.loads(create[3])["metadata"], {"name": "label", "owner": "test"})


if __name__ == "__main__":
    unittest.main()
