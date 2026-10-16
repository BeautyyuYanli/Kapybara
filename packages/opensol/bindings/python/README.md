# Python 集成

OpenSOL（Open Sandbox Operation Layer）是 Rust 核心与适配器的薄绑定，不依赖 Python Docker/E2B SDK，也不反向调用 `kapy.*`。要求 Python 3.12+；PyO3 使用 `abi3-py312`。`import opensol` 提供释放 GIL 的阻塞接口，`from opensol import aio` 提供使用 `pyo3-async-runtimes` 的原生异步接口，不用线程池包装同步调用。两者复用同一核心、DTO、异常与能力含义。

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
- `ProcessManager`：除构造、backend 和 capabilities 外，只提供 `probe/list/get/spawn`。`get(ProcessRef(backend, id, pty=None))` 本地绑定轻量 Process 资源，单任务操作经由该资源执行；存在性与权限由后续 I/O 检查。

每层都提供 `.backend` 和 `.capabilities()`，查询不做 I/O。能力字典除布尔项外，`ProcessCapabilities.supported_signals` 是信号编号列表；`LifecycleCapabilities.stop_preserves_memory` 表达 stop 是否保留进程内存，E2B 为 true，Docker、Kubernetes 与 OpenShell 为 false。工厂不需要全局注册：

| 工厂 | 返回 |
| --- | --- |
| `local()`、`ssh(destination, ...)`、`docker_connection(container)` | `Connection` |
| `docker(image, ...)` | `Docker`：`.lifecycle`、`.connection(resource)` |
| `e2b(api_key, ...)` | `E2b`：`.lifecycle`、`.envd(resource)` |
| `openshell(endpoint, workspace=..., workload_template=..., ...)` | `OpenShell`：`.lifecycle`、`.connection(resource)` |
| `agent_sandbox(namespace, pod_template_json, ...)` | `Lifecycle` |
| `openshell_connection(endpoint, sandbox, ...)`、`sandboxd(endpoint, ...)` | `Connection` |
| `pueue(connection, config_file=..., ...)` | `ProcessManager`，包装注入的连接 |
| `envd(endpoint, ...)`、E2b 的 `.envd(resource)` | 同一个原生 envd 客户端，其 `.connection` / `.processes` 提供两层接口 |

Docker HostConfig、Kubernetes PodTemplate/PVC 参数分别通过原生 JSON 字符串传递，不把供应商字段混入公共 `create()`。OpenShell 可选命名 workload template，或通过 `spec_protobuf` 传原生 SandboxSpec 编码；TLS 参数为 PEM 内容，不是文件路径。支持的信号编号为 2、9、15，具体后端仍可能返回 Unsupported。

`aio` 中的配置型工厂（local、ssh、docker、docker_connection、pueue、envd、e2b、agent_sandbox）是普通函数；`aio.sandboxd`、`aio.openshell`、`aio.openshell_connection` 建立网络连接，需要 await。后端 `.lifecycle` 提供异步生命周期；Docker/OpenShell 的 `.connection(resource)` 是无需 I/O 的普通方法；E2b 的 `.envd(resource)` 只读发现需要 await。envd 的 `.connection` / `.processes` 分别返回异步接口。同步入口的所有 I/O 都是阻塞调用。

后端对象保留原始 Engine/gateway/HTTP 客户端、凭据、作用域与超时，不重复转发生命周期方法。E2B 的 envd_endpoint、sandbox_domain、request_timeout 在 `e2b()` 配置，连接资源时无需再次提供 API key 或 endpoint。工厂不隐式 create/start/probe、不恢复暂停沙盒、不启动 daemon。公共参数校验和能力检查复用 Rust 客户端，Python 仅做语言参数、awaitable 和异常转换。

`aio` 的 Connection 与 Lifecycle 方法，以及 ProcessManager 的所有 I/O 方法都可 await。部分方法直接返回原生 awaitable，不能用 `inspect.iscoroutinefunction` 判断能否 await；在运行中的 asyncio loop 内调用它们。公开类型声明使用 `Awaitable[T]` 或 `async def` 准确表达返回值。

## 原生任务和输出资源

进程任务由实际的 pueued/envd 创建、持有和回收。OpenSOL 不托管 daemon，不增加执行队列或后台日志服务。`probe()` 做真实管理请求，返回 ManagerInfo；Pueue 可报告 CLI 版本，其他无法确认的版本为 None。`list()` 只返回原生当前保留的 ProcessInfo，不回传任务环境；Pueue 的 group 用于创建默认值和 list 过滤，不是权限边界。ProcessRef.pty 是可保存的 `bool | None` 模式提示，由适配器决定操作是否需要模式；envd List 返回 None，spawn 返回 True/False，Pueue 返回 False。envd 未知模式仍可 inspect、signal、kill、wait 和订阅，但 send_input、close_stdin、resize 返回 UnsupportedError，不发送猜测的 RPC；应保存 spawn 引用，或在已知模式时显式传 pty 参数。Pueue send 只需 task ID，重建的 `ProcessRef("pueue", id)` 仍可发送输入。

同步资源方法是阻塞调用；异步资源的相同 I/O 方法可 await，DTO 与异常相同。get、ref、output 属性只构造绑定对象：

```python
from opensol import aio, OutputCursor

async def read_pages(config_file, pueue_directory):
    manager = aio.pueue(aio.local(), config_file=config_file,
                         pueue_directory=pueue_directory)
    await manager.probe()
    handle = await manager.spawn(["python3", "-c", "print('resource')"])
    process = manager.get(handle.process)
    await process.wait(timeout=5)
    page = await process.output.read(limit=4096)
    for chunk in page.chunks:
        print(chunk.channel, chunk.data)
    saved = page.next_cursor.value
    page = await process.output.read(cursor=OutputCursor(saved))
    print(page.at_end, (await process.inspect()).state)
    await handle.aclose()
```

Process 提供 inspect、wait、pause、start、restart、kill、remove、signal、send_input、close_stdin、resize。这些都委托原生管理器，资源不缓存状态；handle 的 outcome 缓存仅属于原来的那次订阅。Pueue start 可强制启动 Queued/Stashed 并绕过队列限制和依赖；restart 仅用于终结任务，原地保留 ID、覆盖上次日志。remove 尊重原生状态/依赖，不暗中 kill；kill 回执不等于已经终结，仍需 inspect/wait。envd 只有原生 kill，pause/start/restart/remove 返回 Unsupported。

`Process.wait()` 和 `ProcessHandle.wait()` 返回共享的 `ProcessOutcome`，表示已确认终结的任务，至少有 exit 或 native_result 一项可用。`outcome.exit` 是 `ExitStatus | None`，`outcome.native_result` 是脱敏的原生结果名称。Pueue Success/Failed 保留真实退出码，Killed、FailedToSpawn 等可以没有退出码；任务终态通过正常值返回。`ProcessInfo.outcome` 统一表达终态，非终态为 None；envd 通过原生完成事件提供含真实 ExitStatus 的 outcome。`Connection.exec()` 的 ExecResult 和 ExitStatus 保持原有含义。

`process.output.stat()` 返回 channels、size、version；version 是后端可提供的不透明输出代次标识，None 只表示没有可报告的标识，任务状态由 ProcessInfo 表达。`read(cursor=None, limit=65536)` 返回 chunks、next_cursor、at_end。chunks.data 是 bytes，总量最多 limit（允许 1 至 1048576）；Pueue 只有 combined。读取立即返回当前观测的日志范围，at_end 仅为暂时读到尾部，不能代表任务结束。额外订阅只通过 `process.output.subscribe()` 建立，要求 reconnect 能力并调用原生重连，返回独立 ProcessHandle；读取和订阅互不推进对方的位置。订阅不要求 output_resource 能力，Rust/Python 均只公开这组绑定资源的输出入口。

Pueue 需显式传 pueue_directory，指向注入 Connection 目标上的 daemon 数据目录，且与 config/profile 对应；要求 Linux 和 GNU stat/dd。Local、SSH、Docker 都经过同一 Connection 在目标侧读取一个已打开的原生日志，并按限定字节取得数据；不把远端路径当成本机路径。probe 和调用时检查依赖/权限，capabilities.output_resource 只表示已配置访问，不缓存探测结果。未配置及 envd 的 stat/read 返回 Unsupported。CLI 只能提供非结构化失败时保留脱敏 Backend；Connection 已确定的结构化错误种类不被改写。

cursor.value 是可保存的不透明字符串，仅在同一目标、任务和日志版本内有效，不包含凭据和调用者指定的日志路径。Queued/Stashed 尚未开始时返回空页，size=0、version=None，不读取旧日志。Queued 的初始空 cursor 可在同一排队执行开始后继续使用；无法确认同次执行的转换报告 Conflict。任务原地重启、可检测的文件替换/截断返回 Conflict，清理任务/日志返回 NotFound，非法格式或 limit 返回 InvalidArgument，不自动重置。原生日志在请求之间截断后重新写长时，文件身份/大小无法证明其完整历史；它不是不可变归档。

## 完成式命令

```python
import opensol as sdk

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

异步短命令与同步命令返回相同的 `ExecResult`：

```python
import asyncio
from opensol import aio

async def main():
    connection = aio.local()
    result = await connection.exec(["printf", "%s", "hello"], timeout=5)
    print(result.stdout)

asyncio.run(main())
```

## 组合 E2B 与 envd

以下示例会创建真实云资源；凭据和模板由调用方提供：

```python
import os
import opensol as sdk

key = os.environ["E2B_API_KEY"]
backend = sdk.e2b(key)
lifecycle = backend.lifecycle
resource = lifecycle.create()
try:
    native = backend.envd(resource)
    result = native.connection.exec(["printf", "%s", "short command"], timeout=10)
    handle = native.processes.spawn(["python3", "-c", "print('managed')"])
    while (event := handle.next_event(timeout=10)) is not None:
        if event.outcome is not None:
            break
        print(event.channel, event.data)
finally:
    lifecycle.delete(resource)
```

envd 的两种公共视图独立使用。`backend.envd(resource)` 只读发现连接配置，不隐式恢复暂停沙盒；aio 对应 `await backend.envd(resource)`。直接 `envd(endpoint, access_token=...)` 不需要 E2B 生命周期。

## 输出订阅与取消

`ProcessHandle.next_event(timeout=...)` 返回一条字节输出或完成事件；订阅结束后返回 None。正常输出事件 `.channel` 为 stdout/stderr/terminal/combined，`.data` 为 bytes，`.outcome` 为 None；完成事件 `.outcome` 是 ProcessOutcome，channel 为 None、data 为空 bytes。缺少完成证据的意外断流报告 TransportError。Pueue 在读取可用日志后交付完成事件；权限、传输、协议和日志读取失败仍返回 SDK 异常。

`spawn()` 直接返回带初始输出订阅的 ProcessHandle，保留 envd Start 的原始流；`manager.get(handle.process)` 只绑定任务，不消费订阅。`.next_event()` 的等待超时只中止本次等待，保留订阅，可以继续读取。`.wait()` 消费并丢弃剩余输出，返回并缓存这次订阅的 ProcessOutcome；需要日志时先逐条读取。同一任务 ID 重启后，已有 handle 的完成缓存仍属于原来的执行，Process.wait 则查询原生管理器。`.disconnect()` 释放订阅、唤醒正在等待的读取线程，但不杀死进程。可保存 `.process`，再用 `manager.get(saved_ref).output.subscribe()` 按原生能力重新订阅。每个 handle 的事件读取串行化；并发消费应在应用层扇出。

异步 handle 使用 `await handle.next_event(timeout=...)`、`await handle.wait()` 和 `await handle.aclose()`，并支持 `async for`。`next_event` 的 timeout 从该读取获得消费权后开始；排在其他读取后面的时间不计入其中。取消读取、取消 wait 或等待超时不会关闭订阅；Rust 已消费但 Python 尚未收到的事件仍保留，下次读取保持顺序。每个 handle 至多保留一条待交付事件，不预取或后台分发。`aclose` 可重复调用并唤醒阻塞读取；它不发信号，不删除环境。

```python
from opensol import aio

async def watch(config_file):
    manager = aio.pueue(aio.local(), config_file=config_file)
    handle = await manager.spawn(["python3", "-c", "print('managed')"])
    try:
        async for event in handle:
            if event.outcome is not None:
                print("completed", event.outcome)
            else:
                print(event.channel, event.data)
    finally:
        await handle.aclose()
    # 若需主动终止仍在运行的任务：await manager.get(handle.process).signal(15)
```

取消 exec、create、spawn 等 awaitable 仅取消客户端等待，原生清理能力由后端决定；这不证明远端未执行或已经停止。SDK 不自动重试结果不确定的副作用操作。需要终止请显式 signal，需要销毁请显式 delete；丢弃客户端或订阅不会自动删除环境。

`process.wait(timeout=...)` 超时不代表进程已终止。明确停止请用 `process.kill()` 或支持的 signal；删除环境请显式 `Lifecycle.delete()`。包装对象没有自动销毁远端环境的上下文管理器。

Pueue 订阅完成后才返回合并日志，并要求 CLI 与 daemon 在同一执行环境、`client.read_local_logs: true`；独立资源查询可读取运行中的原生日志。它不支持 PTY 或关闭 stdin；输入限 UTF-8 且无 NUL，在 Connection I/O 前按 UnsupportedError 拒绝不符合的载荷。`binary_stdin=true` 保证任意 bytes，false 表示具体限制由适配器判断。envd 原样传递 bytes，可以实时输出和 PTY，但不承诺完整历史或 daemon 重启恢复。详见[能力矩阵](../../README.md#第三层的独立能力)。

## 错误与验证

`SdkError` 的具体子类为 InvalidArgumentError、UnsupportedError、NotFoundError、ConflictError、PermissionError、TimeoutError、TransportError、BackendError。它们带 `.backend`、`.operation`、`.kind`；参数转换阶段的 SDK 错误可以使用 `python` backend。Python 类型错误仍是普通 TypeError，而不是后端故障。

超时与断连都可能留下正在运行的远端操作；SDK 不自行重做不确定的副作用。不将异常字符串当成日志存档，原生输出通过正常返回值消费。

绑定验证由 SDK 根目录的 `Dockerfile.tests` / `scripts/check.sh` 执行，测试安装后的 wheel，而不导入未编译的源码。真实 Local/Pueue/Docker 验证同步与异步进程、字节与资源边界；本地 E2B HTTP fixture 验证资源包装和异常转换，envd ConnectRPC fixture 验证二进制 stdin/关闭、PTY 维度与终端字节及 resize。异步覆盖事件循环推进、读取/等待取消、输出及完成事件交付前取消、无退出码的终态、意外断流、显式关闭和网络工厂的真实 gRPC 握手。供应商 RPC/TLS 的固定 schema 契约另由 Rust fixture 验证，不调用真实云服务。测试脚本从固定的上游 schema 在临时目录生成 Python protobuf 类型，grpcio-tools 仅为测试依赖。
