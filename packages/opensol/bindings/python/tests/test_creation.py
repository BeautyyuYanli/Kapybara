"""Unified construction on the installed wheel, with local resources and fixed protocol data."""

import gc
import inspect
import os
import unittest

import opensol as sdk


class DataTests(unittest.TestCase):
    def test_backend_capabilities_describe_bindings_without_opening_clients(self):
        cases = [
            (sdk.LocalConfig(), (None, "forbidden", None, "forbidden")),
            (sdk.SshConfig("unreachable", identity_file="/missing/key"), (None, "forbidden", None, "forbidden")),
            (sdk.DockerConfig(), ("forbidden", "required", None, "forbidden")),
            (sdk.E2bConfig("unused"), ("forbidden", "required", "required", "forbidden")),
            (sdk.AgentSandboxConfig(kubeconfig="/missing/config"), ("forbidden", None, None, "forbidden")),
            (sdk.OpenShellConfig("https://unreachable", ca_certificate="not-a-certificate"), ("forbidden", "required", None, "forbidden")),
            (sdk.EnvdConfig("http://127.0.0.1:1"), (None, "forbidden", "forbidden", "forbidden")),
            (sdk.SandboxdConfig("http://127.0.0.1:1"), (None, "forbidden", None, "forbidden")),
            (sdk.PueueConfig(config_file="/missing/config"), (None, None, "forbidden", "required")),
            (sdk.TmuxConfig("/missing/socket"), (None, None, "forbidden", "required")),
        ]
        for config, expected in cases:
            with self.subTest(backend=config.backend):
                caps: sdk.BackendCapabilities = config.capabilities()
                self.assertEqual(caps, dict(zip(("sandboxes", "connection", "processes", "connection_dependency"), expected)))
                caps["processes"] = None
                self.assertEqual(config.capabilities()["processes"], expected[2])

    def test_nested_capabilities_are_local_independent_and_shared_by_sync_async_views(self):
        config = sdk.E2bConfig("unused", api_endpoint="http://127.0.0.1:1")
        backend = sdk.AsyncBackend(config)
        self.assertEqual(backend.capabilities(), config.capabilities())
        self.assertEqual(backend.to_backend().capabilities(), config.capabilities())
        manager = sdk.AsyncProcessManager(backend, target=sdk.SandboxRef("e2b", "saved"))
        upper: sdk.ProcessManagerCapabilities = manager.capabilities()
        self.assertEqual(upper["spawn_modes"], [dict(pty=False, stdin=False), dict(pty=False, stdin=True),
                                               dict(pty=True, stdin=False), dict(pty=True, stdin=True)])
        self.assertEqual(upper["process"]["supported_signals"], [9, 15])
        self.assertEqual(upper["process"]["kill"], "request_termination")
        self.assertEqual(upper["process"]["completion"], "os_exit")
        upper["process"]["supported_signals"].clear()
        upper["spawn_modes"].clear()
        upper["output"]["subscribe"]["reconnect"]["channels"].clear()
        self.assertEqual(manager.capabilities(), manager.to_process_manager().capabilities())
        self.assertEqual(manager.capabilities()["process"]["supported_signals"], [9, 15])
        process = manager.get(sdk.ProcessRef("envd", "42"))
        control: sdk.ProcessCapabilities = process.capabilities()
        self.assertIsNone(control["input"])
        self.assertNotIn("list", control)
        self.assertNotIn("output", control)
        self.assertIsNone(process.output.capabilities()["subscribe"]["initial"])
        self.assertEqual(process.output.capabilities()["subscribe"]["reconnect"]["channels"], ["stdout", "stderr", "terminal"])
        lifecycle = sdk.AsyncSandboxManager(backend)
        sandbox = lifecycle.get(sdk.SandboxRef("e2b", "saved"))
        self.assertEqual(sandbox.capabilities(), lifecycle.capabilities()["sandbox"])
        self.assertEqual(sandbox.capabilities()["stop"], dict(preserves_memory=True))
        self.assertEqual(sandbox.capabilities(), lifecycle.to_sandbox_manager().get(sandbox.ref).capabilities())
        local = sdk.Connection(sdk.LocalConfig()).capabilities()
        self.assertEqual(local["stdin"], dict(content="bytes", max_bytes=None))
        self.assertEqual(local["cancellation"], "request_target_termination")
        ssh = sdk.Connection(sdk.SshConfig("unreachable")).capabilities()
        self.assertEqual(ssh["cancellation"], "no_target_termination_guarantee")
        self.assertIsNone(sdk.Connection(sdk.SandboxdConfig("http://127.0.0.1:1")).capabilities()["stdin"])

    def test_configuration_and_spec_serialization_are_independent_of_runtime_handles(self):
        configs = [sdk.LocalConfig(), sdk.SshConfig("unreachable"), sdk.DockerConfig(),
                   sdk.E2bConfig("api-secret"), sdk.EnvdConfig("http://127.0.0.1:1", access_token="envd-secret"),
                   sdk.AgentSandboxConfig("work"), sdk.PueueConfig(config_file="/target/pueue.yml"),
                   sdk.TmuxConfig("/target/tmux.sock"), sdk.SandboxdConfig("https://unreachable", bearer_token="bearer-secret"),
                   sdk.OpenShellConfig("https://unreachable", client_certificate="cert-secret", client_key="key-secret")]
        specs = [sdk.DockerSandboxSpec("image", host_config={"Memory": 1024}),
                 sdk.E2bSandboxSpec(template="custom", sandbox_timeout=17, metadata={"project": "test"}),
                 sdk.AgentSandboxSpec({"spec": {"containers": [{"name": "main", "image": "image"}]}},
                                      volume_claim_templates=[{"metadata": {"name": "data"}}]),
                 sdk.OpenShellSandboxSpec(workload_template="template"),
                 sdk.OpenShellSandboxSpec(spec_protobuf=b"\x0a\x01x")]
        for value in configs + specs:
            with self.subTest(value=type(value).__name__):
                base = sdk.BackendConfig if isinstance(value, sdk.BackendConfig) else sdk.SandboxSpec
                self.assertEqual(base.from_json(value.to_json()), value)
                self.assertEqual(type(value).from_dict(value.to_dict()), value)
                self.assertFalse(inspect.isawaitable(value))
        for value in configs:
            self.assertNotIn("-secret", repr(value))
        original = sdk.DockerSandboxSpec("image", labels={"project": "original"})
        saved = original.to_dict()
        saved["spec"]["labels"]["project"] = "changed"
        self.assertEqual(original.labels, {"project": "original"})

    def test_local_type_and_mutual_exclusion_errors(self):
        invalid = [lambda: sdk.BackendConfig(), lambda: sdk.SandboxSpec(),
                   lambda: sdk.SshConfig(12), lambda: sdk.E2bConfig(api_key=12),
                   lambda: sdk.DockerSandboxSpec("image", command="sleep"),
                   lambda: sdk.DockerSandboxSpec("image", host_config="{}"),
                   lambda: sdk.AgentSandboxSpec("{}"),
                   lambda: sdk.E2bSandboxSpec(sandbox_timeout=True),
                   lambda: sdk.E2bSandboxSpec(labels={"bad": 12}),
                   lambda: sdk.OpenShellSandboxSpec(),
                   lambda: sdk.OpenShellSandboxSpec(spec_protobuf=b"", workload_template="both"),
                   lambda: sdk.SandboxdConfig("https://unreachable", client_certificate="cert"),
                   lambda: sdk.PueueConfig(request_timeout=float("inf")),
                   lambda: sdk.PueueConfig(request_timeout=10**1000),
                   lambda: sdk.TmuxConfig("/tmp/x", request_timeout=0),
                   lambda: sdk.BackendConfig.from_dict({"backend": "agent-box", "config": {}})]
        for operation in invalid:
            with self.subTest(operation=operation), self.assertRaises(sdk.InvalidArgumentError):
                operation()

    def test_old_provider_factories_and_containers_are_absent(self):
        for module in (sdk,):
            for name in ("local", "ssh", "docker", "docker_connection", "envd", "e2b",
                         "agent_sandbox", "pueue", "tmux", "sandboxd", "openshell",
                         "openshell_connection", "Docker", "E2b", "Envd", "OpenShell"):
                self.assertFalse(hasattr(module, name), (module.__name__, name))


class ConstructionTests(unittest.TestCase):
    def test_async_clients_and_conversions_need_neither_network_nor_running_loop(self):
        backend = sdk.AsyncBackend(sdk.E2bConfig("key", api_endpoint="http://127.0.0.1:1"))
        connection = sdk.AsyncConnection(backend, target=sdk.SandboxRef("e2b", "saved"))
        processes = sdk.AsyncProcessManager(backend, target=sdk.SandboxRef("e2b", "saved"))
        sandboxes = sdk.AsyncSandboxManager(backend)
        for view, to_sync, to_async in (
            (backend, "to_backend", "to_async_backend"),
            (connection, "to_connection", "to_async_connection"),
            (processes, "to_process_manager", "to_async_process_manager"),
            (sandboxes, "to_sandbox_manager", "to_async_sandbox_manager"),
        ):
            blocking = getattr(view, to_sync)()
            restored = getattr(blocking, to_async)()
            self.assertEqual(restored.backend, view.backend)
            self.assertFalse(inspect.isawaitable(view))
        self.assertTrue(connection.capabilities()["stdin"])
        self.assertIsInstance(processes.get(sdk.ProcessRef("envd", "42", False)), sdk.AsyncProcess)
        self.assertIsInstance(sandboxes.get(sdk.SandboxRef("e2b", "saved")), sdk.AsyncSandbox)
        for config, target in ((sdk.SandboxdConfig("http://127.0.0.1:1"), None),
                               (sdk.OpenShellConfig("http://127.0.0.1:1"), sdk.SandboxRef("openshell", "saved"))):
            client = sdk.AsyncConnection(config, target=target)
            self.assertEqual(client.to_connection().backend, client.backend)

    def test_binding_errors_are_reported_before_native_connections(self):
        for config in (sdk.OpenShellConfig("http://127.0.0.1:1"),
                       sdk.E2bConfig("key", api_endpoint="http://127.0.0.1:1"), sdk.DockerConfig()):
            for target in (None, sdk.SandboxRef("other", "id"), sdk.SandboxRef(config.backend, "")):
                with self.subTest(config=config, target=target), self.assertRaises(sdk.InvalidArgumentError):
                    sdk.Connection(config, target=target)
        for config in (sdk.LocalConfig(), sdk.SshConfig("unreachable"), sdk.EnvdConfig("http://127.0.0.1:1"),
                       sdk.SandboxdConfig("http://127.0.0.1:1"), sdk.PueueConfig(), sdk.TmuxConfig("/tmp/x")):
            with self.subTest(config=config), self.assertRaises(sdk.UnsupportedError):
                sdk.SandboxManager(config)
        for config in (sdk.PueueConfig(), sdk.TmuxConfig("/tmp/x")):
            with self.assertRaises(sdk.InvalidArgumentError):
                sdk.Backend(config)
            with self.assertRaises(sdk.InvalidArgumentError):
                sdk.ProcessManager(config)
        local = sdk.Connection(sdk.LocalConfig())
        context = sdk.Backend(sdk.TmuxConfig("/tmp/opensol-never-created.sock"), connection=local)
        manager = sdk.ProcessManager(context)
        self.assertTrue(manager.capabilities()["list"])
        with self.assertRaises(sdk.InvalidArgumentError):
            sdk.ProcessManager(context, connection=local)
        with self.assertRaises(sdk.InvalidArgumentError):
            sdk.Backend(sdk.LocalConfig(), connection=local)
        with self.assertRaises(sdk.InvalidArgumentError):
            sdk.Connection({}, target="id")
        del context, local
        gc.collect()
        self.assertIsInstance(manager.get(sdk.ProcessRef("tmux", "saved", True)), sdk.Process)

    def test_docker_specs_are_complete_and_structured_host_config_reaches_engine(self):
        context = sdk.Backend(sdk.DockerConfig())
        manager = sdk.SandboxManager(context)
        image = os.environ["OPENSOL_TEST_IMAGE"]
        resources = []
        try:
            for index, directory in enumerate(("/tmp", "/")):
                sandbox = manager.create(sdk.DockerSandboxSpec(image,
                    command=["sleep", "infinity"], env={"SPEC": str(index)}, working_dir=directory,
                    host_config={"ReadonlyRootfs": index == 0}, labels={"case": str(index)}))
                resources.append(sandbox)
                self.assertEqual(sandbox.inspect().state, "stopped")
                sandbox.start()
                connection = sdk.Connection(context, target=sandbox.ref)
                self.assertEqual(connection.shell("printf '%s:%s' \"$SPEC\" \"$PWD\"").stdout,
                                 f"{index}:{directory}".encode())
                self.assertEqual(connection.shell("touch /tmp/opensol-native-write").success, index == 1)
            with self.assertRaises(sdk.InvalidArgumentError):
                manager.create(sdk.E2bSandboxSpec())
            saved = resources[0].ref
            del context, manager
            gc.collect()
            self.assertEqual(resources[0].inspect().labels["case"], "0")
            self.assertEqual(sdk.SandboxManager(sdk.DockerConfig()).get(saved).inspect().state, "running")
        finally:
            for sandbox in reversed(resources):
                sandbox.delete()


class AsyncConstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_views_share_the_context_and_connection_semantics(self):
        context = sdk.AsyncBackend(sdk.LocalConfig())
        connection = sdk.AsyncConnection(context)
        sync_connection = connection.to_connection()
        converted = sync_connection.to_async_connection()
        direct = sdk.Connection(context.to_backend())
        del context
        gc.collect()
        self.assertEqual((await connection.exec(["printf", "async"])).stdout, b"async")
        self.assertEqual(sync_connection.exec(["printf", "sync"]).stdout, b"sync")
        self.assertEqual((await converted.exec(["printf", "converted"])).stdout, b"converted")
        self.assertEqual(direct.exec(["printf", "direct"]).stdout, b"direct")
        tmux = sdk.AsyncBackend(sdk.TmuxConfig("/tmp/opensol-never-created.sock"), connection=connection)
        manager = sdk.ProcessManager(tmux.to_backend()).to_async_process_manager()
        with self.assertRaises(sdk.InvalidArgumentError):
            sdk.AsyncProcessManager(tmux, connection=connection)
        with self.assertRaises(sdk.InvalidArgumentError):
            sdk.AsyncConnection(sdk.OpenShellConfig("http://127.0.0.1:1"))
        with self.assertRaises(sdk.UnsupportedError):
            sdk.AsyncProcessManager(sdk.DockerConfig())
        self.assertTrue(manager.capabilities()["list"])
