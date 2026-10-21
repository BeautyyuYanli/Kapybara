"""Unified construction on the installed wheel, with local resources and fixed protocol data."""

import asyncio
import gc
import inspect
import os
import unittest

import opensol as sdk
from opensol import aio


class DataTests(unittest.TestCase):
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
                self.assertIs(getattr(aio, type(value).__name__), type(value))
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
        for module in (sdk, aio):
            for name in ("local", "ssh", "docker", "docker_connection", "envd", "e2b",
                         "agent_sandbox", "pueue", "tmux", "sandboxd", "openshell",
                         "openshell_connection", "Docker", "E2b", "Envd", "OpenShell"):
                self.assertFalse(hasattr(module, name), (module.__name__, name))


class ConstructionTests(unittest.TestCase):
    def test_binding_errors_are_reported_before_native_connections(self):
        for config in (sdk.OpenShellConfig("http://127.0.0.1:1"),
                       sdk.E2bConfig("key", api_endpoint="http://127.0.0.1:1"), sdk.DockerConfig()):
            for target in (None, sdk.SandboxRef("other", "id"), sdk.SandboxRef(config.backend, "")):
                with self.subTest(config=config, target=target), self.assertRaises(sdk.InvalidArgumentError):
                    sdk.Connection.open(config, target=target)
        for config in (sdk.LocalConfig(), sdk.SshConfig("unreachable"), sdk.EnvdConfig("http://127.0.0.1:1"),
                       sdk.SandboxdConfig("http://127.0.0.1:1"), sdk.PueueConfig(), sdk.TmuxConfig("/tmp/x")):
            with self.subTest(config=config), self.assertRaises(sdk.UnsupportedError):
                sdk.SandboxManager.open(config)
        for config in (sdk.PueueConfig(), sdk.TmuxConfig("/tmp/x")):
            with self.assertRaises(sdk.InvalidArgumentError):
                sdk.Backend.open(config)
            with self.assertRaises(sdk.InvalidArgumentError):
                sdk.ProcessManager.open(config)
        local = sdk.Connection.open(sdk.LocalConfig())
        context = sdk.Backend.open(sdk.TmuxConfig("/tmp/opensol-never-created.sock"), connection=local)
        manager = sdk.ProcessManager.open(context)
        self.assertTrue(manager.capabilities()["list"])
        with self.assertRaises(sdk.InvalidArgumentError):
            sdk.ProcessManager.open(context, connection=local)
        with self.assertRaises(sdk.InvalidArgumentError):
            sdk.Backend.open(sdk.LocalConfig(), connection=local)
        with self.assertRaises(sdk.InvalidArgumentError):
            sdk.Connection.open({}, target="id")
        del context, local
        gc.collect()
        self.assertIsInstance(manager.get(sdk.ProcessRef("tmux", "saved", True)), sdk.Process)

    def test_docker_specs_are_complete_and_structured_host_config_reaches_engine(self):
        context = sdk.Backend.open(sdk.DockerConfig())
        manager = sdk.SandboxManager.open(context)
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
                connection = sdk.Connection.open(context, target=sandbox.ref)
                self.assertEqual(connection.shell("printf '%s:%s' \"$SPEC\" \"$PWD\"").stdout,
                                 f"{index}:{directory}".encode())
                self.assertEqual(connection.shell("touch /tmp/opensol-native-write").success, index == 1)
            with self.assertRaises(sdk.InvalidArgumentError):
                manager.create(sdk.E2bSandboxSpec())
            saved = resources[0].ref
            del context, manager
            gc.collect()
            self.assertEqual(resources[0].inspect().labels["case"], "0")
            self.assertEqual(sdk.SandboxManager.open(sdk.DockerConfig()).get(saved).inspect().state, "running")
        finally:
            for sandbox in reversed(resources):
                sandbox.delete()


class AsyncConstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_opens_are_lazy_and_share_the_sync_context_and_connection_semantics(self):
        for opening in (aio.Backend.open(sdk.LocalConfig()), aio.Connection.open(sdk.LocalConfig()),
                        aio.SandboxManager.open(sdk.OpenShellConfig("http://127.0.0.1:1")),
                        aio.ProcessManager.open(sdk.EnvdConfig("http://127.0.0.1:1"))):
            self.assertTrue(inspect.iscoroutine(opening))
            task = asyncio.create_task(opening)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        context = await aio.Backend.open(sdk.LocalConfig())
        connection = await asyncio.create_task(aio.Connection.open(context))
        sync_connection = sdk.Connection.open(context)
        del context
        gc.collect()
        self.assertEqual((await connection.exec(["printf", "async"])).stdout, b"async")
        self.assertEqual(sync_connection.exec(["printf", "sync"]).stdout, b"sync")
        tmux = await aio.Backend.open(sdk.TmuxConfig("/tmp/opensol-never-created.sock"), connection=connection)
        manager = await aio.ProcessManager.open(tmux)
        with self.assertRaises(sdk.InvalidArgumentError):
            await aio.ProcessManager.open(tmux, connection=connection)
        with self.assertRaises(sdk.InvalidArgumentError):
            await aio.Connection.open(sdk.OpenShellConfig("http://127.0.0.1:1"))
        with self.assertRaises(sdk.UnsupportedError):
            await aio.ProcessManager.open(sdk.DockerConfig())
        self.assertTrue(manager.capabilities()["list"])
