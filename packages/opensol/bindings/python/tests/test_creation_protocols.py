"""Installed binding specs reach fixed native schemas and real kubectl unchanged."""

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import grpc
import opensol as sdk
from opensol import aio
import openshell_pb2 as native
import datamodel_pb2 as datamodel


class OpenShellFixture:
    def setUp(self):
        self.requests = []
        self.server = grpc.server(ThreadPoolExecutor(max_workers=2))

        def create(request, context):
            self.assertEqual(dict(context.invocation_metadata())["authorization"], "Bearer fixture-key")
            self.requests.append(request)
            return native.SandboxResponse(sandbox=native.Sandbox(metadata=datamodel.ObjectMeta(name=request.name or "native-name")))

        self.server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("openshell.v1.OpenShell", {
            "CreateSandbox": grpc.unary_unary_rpc_method_handler(create,
                request_deserializer=native.CreateSandboxRequest.FromString,
                response_serializer=native.SandboxResponse.SerializeToString),
        }),))
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.addCleanup(lambda: self.server.stop(0).wait(2))
        self.config = sdk.OpenShellConfig(f"http://127.0.0.1:{port}", workspace="team", bearer_token="fixture-key")


class OpenShellTests(OpenShellFixture, unittest.TestCase):
    def test_template_and_native_protobuf_are_per_create_requests(self):
        backend = sdk.Backend.open(self.config)
        manager = sdk.SandboxManager.open(backend)
        self.assertEqual(self.requests, [], "open never probes the gateway")
        first = manager.create(sdk.OpenShellSandboxSpec(workload_template="template-a", name="first", labels={"project": "a"}))
        native_spec = native.SandboxSpec(command=["python3", "main.py"], environment={"ITEM": "b"})
        second = manager.create(sdk.OpenShellSandboxSpec(spec_protobuf=native_spec.SerializeToString(), name="second"))
        self.assertEqual((first.ref.id, second.ref.id), ("first", "second"))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0].workload_template, "template-a")
        self.assertFalse(self.requests[0].HasField("spec"))
        self.assertEqual(dict(self.requests[0].labels), {"project": "a"})
        self.assertEqual(self.requests[1].spec, native_spec)
        self.assertFalse(self.requests[1].workload_template)
        self.assertEqual(dict(self.requests[1].labels), {})
        self.assertTrue(all(request.workspace_scope.workspace == "team" for request in self.requests))
        for spec in (sdk.OpenShellSandboxSpec(spec_protobuf=b"\xff"), sdk.DockerSandboxSpec("image")):
            with self.assertRaises(sdk.InvalidArgumentError):
                manager.create(spec)
        self.assertEqual(len(self.requests), 2)


class AsyncOpenShellTests(OpenShellFixture, unittest.IsolatedAsyncioTestCase):
    async def test_native_spec_serialization_is_shared_by_async_create(self):
        backend = await aio.Backend.open(self.config)
        manager = await aio.SandboxManager.open(backend)
        spec = native.SandboxSpec(command=["sleep", "infinity"])
        sandbox = await manager.create(sdk.OpenShellSandboxSpec(spec_protobuf=spec.SerializeToString(), name="async"))
        self.assertIsInstance(sandbox, aio.Sandbox)
        self.assertEqual(sandbox.ref.id, "async")
        self.assertEqual(self.requests[0].spec, spec)
        with self.assertRaises(sdk.InvalidArgumentError):
            await manager.create(sdk.E2bSandboxSpec())
        self.assertEqual(len(self.requests), 1)


class KubernetesFixture:
    def setUp(self):
        self.requests = []
        requests = self.requests
        group_version = "agents.x-k8s.io/v1beta1"
        group = {"name": "agents.x-k8s.io", "versions": [{"groupVersion": group_version, "version": "v1beta1"}],
                 "preferredVersion": {"groupVersion": group_version, "version": "v1beta1"}}
        resources = {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": group_version,
                     "resources": [{"name": "sandboxes", "singularName": "sandbox", "namespaced": True,
                                    "kind": "Sandbox", "verbs": ["get", "list", "create", "delete"]}]}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, value, status=200):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/api":
                    self.reply({"kind": "APIVersions", "apiVersion": "v1", "versions": ["v1"], "serverAddressByClientCIDRs": []})
                elif path == "/api/v1":
                    self.reply({"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": "v1", "resources": []})
                elif path == "/apis":
                    self.reply({"kind": "APIGroupList", "apiVersion": "v1", "groups": [group]})
                elif path == "/apis/agents.x-k8s.io":
                    self.reply(group)
                elif path == "/apis/" + group_version:
                    self.reply(resources)
                elif path == "/openapi/v3":
                    self.reply({"paths": {"apis/" + group_version: {"serverRelativeURL": "/openapi/v3/apis/" + group_version}}})
                elif path == "/openapi/v3/apis/" + group_version:
                    self.reply({"openapi": "3.0.0", "info": {"title": "fixture", "version": "1.0"}, "paths": {
                        "/apis/agents.x-k8s.io/v1beta1/namespaces/{namespace}/sandboxes/{name}": {"patch": {
                            "x-kubernetes-group-version-kind": {"group": "agents.x-k8s.io", "version": "v1beta1", "kind": "Sandbox"},
                            "parameters": [{"name": "fieldValidation", "in": "query", "schema": {"type": "string"}}],
                            "responses": {"200": {"description": "OK"}},
                        }},
                    }})
                elif path == "/apis/" + group_version + "/namespaces/work/sandboxes":
                    self.reply({"kind": "SandboxList", "apiVersion": group_version, "metadata": {}, "items": []})
                else:
                    self.reply({"kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "NotFound", "code": 404}, 404)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["content-length"])))
                requests.append((self.path, request))
                self.reply(request, 201)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def close():
            server.shutdown()
            worker.join(2)
            server.server_close()

        self.addCleanup(close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        kubeconfig = Path(directory.name) / "config"
        kubeconfig.write_text(json.dumps({"apiVersion": "v1", "kind": "Config",
            "clusters": [{"name": "fixture", "cluster": {"server": f"http://127.0.0.1:{server.server_port}"}}],
            "contexts": [{"name": "fixture", "context": {"cluster": "fixture", "user": "fixture"}}],
            "current-context": "fixture", "users": [{"name": "fixture", "user": {}}]}))
        self.config = sdk.AgentSandboxConfig("work", kubeconfig=str(kubeconfig))


class KubernetesTests(KubernetesFixture, unittest.TestCase):
    def test_structured_pod_pvc_and_service_are_forwarded_per_create(self):
        manager = sdk.SandboxManager.open(self.config)
        self.assertEqual(manager.list(), [], "listing does not require a creation template")
        for index in range(2):
            pod = {"spec": {"containers": [{"name": "main", "image": str(index)}]}}
            volumes = [{"metadata": {"name": "data"}, "spec": {"accessModes": ["ReadWriteOnce"]}}] if index == 0 else []
            sandbox = manager.create(sdk.AgentSandboxSpec(pod, volume_claim_templates=volumes, service=index == 0,
                                                         name=f"sandbox-{index}", labels={"item": str(index)}))
            self.assertEqual(sandbox.ref.id, f"sandbox-{index}")
            path, request = self.requests[index]
            self.assertTrue(path.startswith("/apis/agents.x-k8s.io/v1beta1/namespaces/work/sandboxes"))
            self.assertEqual(request["metadata"]["namespace"], "work")
            self.assertEqual(request["metadata"]["labels"], {"item": str(index)})
            self.assertEqual(request["spec"], {"podTemplate": pod, "volumeClaimTemplates": volumes,
                                              "service": index == 0, "operatingMode": "Running"})
        with self.assertRaises(sdk.InvalidArgumentError):
            manager.create(sdk.AgentSandboxSpec({}))
        self.assertEqual(len(self.requests), 2)


class AsyncKubernetesTests(KubernetesFixture, unittest.IsolatedAsyncioTestCase):
    async def test_async_manager_encodes_the_same_structured_spec(self):
        manager = await aio.SandboxManager.open(self.config)
        pod = {"spec": {"containers": [{"name": "main", "image": "async"}]}}
        sandbox = await manager.create(sdk.AgentSandboxSpec(pod, service=False, name="async"))
        self.assertEqual(sandbox.ref.id, "async")
        self.assertEqual(self.requests[0][1]["spec"]["podTemplate"], pod)
        self.assertFalse(self.requests[0][1]["spec"]["service"])
