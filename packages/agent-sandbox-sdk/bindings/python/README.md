# Python 集成

这是 Rust 核心与适配器的薄绑定，不依赖 Python Docker/E2B SDK，也不反向调用 `kapy.*`。要求 Python 3.12+；PyO3 使用 `abi3-py312`。所有网络及进程等待都在释放 GIL 后执行，适合放进调用方自己的线程池。

## 安装

当前包尚未发布到 PyPI，在 SDK 根目录构建安装：

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install 'maturin>=1.10,<2'
maturin develop --manifest-path bindings/python/Cargo.toml
```

发布 wheel 可使用 `maturin build --manifest-path bindings/python/Cargo.toml --release`。协议自动生成，不需要单独安装 protoc。发行包包含 `_native.pyi` 和 `py.typed`；静态类型来自同一公开 Rust 接口的 Python 包装。

## 三层对象

- `Lifecycle`：`create/inspect/start/stop/delete/fork`。`ResourceRef(backend, id)` 可由调用方保存并重建，不包含凭据。
- `Connection`：`exec(argv, cwd=..., env=..., stdin=b"", timeout=...)`、显式 `.shell(script)`。返回 `ExecResult`，不返回后台任务 ID。
- `ProcessManager`：`spawn/status/wait/output/reconnect/send_input/close_stdin/signal/resize`。进程引用使用 `ProcessRef(backend, id, pty=False)`。

每层都提供 `.backend` 和 `.capabilities()`；后者是各能力独立的布尔字典。工厂是普通函数，不需要全局注册：

| 工厂 | 返回 |
| --- | --- |
| `local()`、`ssh(destination, ...)`、`docker_connection(container)` | `Connection` |
| `docker(image, ...)`、`e2b(api_key, ...)`、`agent_sandbox(namespace, pod_template_json, ...)` | `Lifecycle` |
| `openshell(endpoint, workspace=..., workload_template=..., ...)` | `Lifecycle` |
| `openshell_connection(endpoint, sandbox, ...)`、`sandboxd(endpoint, ...)` | `Connection` |
| `pueue(connection, config_file=..., ...)` | `ProcessManager`，包装注入的连接 |
| `envd(endpoint, ...)`、`e2b_envd(api_key, resource, ...)` | 同一个原生 envd 客户端，其 `.connection` / `.processes` 提供两层接口 |

Docker HostConfig、Kubernetes PodTemplate/PVC 参数分别通过原生 JSON 字符串传递，不把供应商字段混入公共 `create()`。OpenShell 可选命名 workload template，或通过 `spec_protobuf` 传原生 SandboxSpec 编码；TLS 参数为 PEM 内容，不是文件路径。支持的信号编号为 2、9、15，具体后端仍可能返回 Unsupported。

## 完成式命令

```python
import agent_sandbox_sdk as sdk

connection = sdk.local()
result = connection.exec(
    ["python3", "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.exit(7)"],
    stdin=b"\x00\xffbytes", timeout=5,
)
assert result.stdout == b"\x00\xffbytes"
assert result.exit_code == 7
assert not result.success
```

stdout/stderr 永远是 `bytes`，解码策略由应用决定。非零退出码不抛异常；本机信号退出使用 `.signal`，`.exit_code` 可为 None。需要 shell 扩展时使用 `.shell()`，argv 不会自动变成 shell 字符串。

## 组合 E2B 与 envd

以下示例会创建真实云资源；凭据和模板由调用方提供：

```python
import os
import agent_sandbox_sdk as sdk

key = os.environ["E2B_API_KEY"]
lifecycle = sdk.e2b(key)
resource = lifecycle.create()
try:
    native = sdk.e2b_envd(key, resource)
    result = native.connection.exec(["printf", "%s", "short command"], timeout=10)
    handle = native.processes.spawn(["python3", "-c", "print('managed')"])
    while (event := handle.next_event(timeout=10)) is not None:
        if event.exit is not None:
            break
        print(event.channel, event.data)
finally:
    lifecycle.delete(resource)
```

这不是三层串行包装：envd 直接实现两种独立接口。`e2b_envd` 只读发现连接配置，不隐式恢复暂停沙盒。直接 `envd(endpoint, access_token=...)` 不需要 E2B 生命周期。

## 输出订阅与取消

`ProcessHandle.next_event(timeout=...)` 返回一条字节输出或退出事件；订阅结束后返回 None。正常输出事件 `.channel` 为 stdout/stderr/terminal/combined，`.data` 为 bytes，`.exit` 为 None；退出事件 `.exit` 是 ExitStatus。缺少原生退出事件的意外断流报告 TransportError，不能当成成功。

`.next_event()` 的等待超时只中止本次等待，保留订阅，可以继续读取。`.wait()` 消费并丢弃剩余输出，返回退出状态；需要日志时先逐条读取。`.disconnect()` 释放订阅、唤醒正在等待的读取线程，但不杀死进程。可保存 `.process`，再用 `ProcessManager.reconnect()` 按原生能力重新订阅。每个 handle 的事件读取串行化；并发消费应在应用层扇出。

`ProcessManager.wait(process, timeout=...)` 同样不代表超时后终止进程。明确停止请用 `.signal(process, 15/9)`；删除环境请显式 `Lifecycle.delete()`。包装对象没有自动销毁远端环境的上下文管理器。

Pueue 首版完成后才返回合并日志，并要求 CLI 与 daemon 在同一执行环境、`client.read_local_logs: true`。它不支持 PTY、二进制 stdin 或关闭 stdin。envd 可以实时输出和 PTY，但不承诺完整历史或 daemon 重启恢复。详见[能力矩阵](../../README.md#第三层的独立能力)。

## 错误与验证

`SdkError` 的具体子类为 InvalidArgumentError、UnsupportedError、NotFoundError、ConflictError、PermissionError、TimeoutError、TransportError、BackendError。它们带 `.backend`、`.operation`、`.kind`；参数转换阶段的 SDK 错误可以使用 `python` backend。Python 类型错误仍是普通 TypeError，而不是后端故障。

超时与断连都可能留下正在运行的远端操作；SDK 不自行重做不确定的副作用。不将异常字符串当成日志存档，原生输出通过正常返回值消费。

绑定验证由 SDK 根目录的 `Dockerfile.tests` / `scripts/check.sh` 执行，测试安装后的 wheel，而不导入未编译的源码。真实 Local/Pueue/Docker 验证进程与字节边界；本地 E2B HTTP fixture 验证资源包装和异常转换，envd ConnectRPC fixture 验证二进制 stdin/关闭、PTY 维度与终端字节及 resize，不调用真实云服务。测试脚本从固定的上游 schema 在临时目录生成 Python protobuf 类型，grpcio-tools 仅为测试依赖。
