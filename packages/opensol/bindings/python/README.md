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
| `tmux(connection, *, socket_path, session_prefix="opensol", program="tmux")` | `ProcessManager`，连接目标机器上已有的 tmux >= 3.5 server |
| `envd(endpoint, ...)`、E2b 的 `.envd(resource)` | 同一个原生 envd 客户端，其 `.connection` / `.processes` 提供两层接口 |

Docker HostConfig、Kubernetes PodTemplate/PVC 参数分别通过原生 JSON 字符串传递，不把供应商字段混入公共 `create()`。OpenShell 可选命名 workload template，或通过 `spec_protobuf` 传原生 SandboxSpec 编码；TLS 参数为 PEM 内容，不是文件路径。支持的信号编号为 2、9、15，具体后端仍可能返回 Unsupported。

`aio` 中的配置型工厂（local、ssh、docker、docker_connection、pueue、tmux、envd、e2b、agent_sandbox）是普通函数；`aio.sandboxd`、`aio.openshell`、`aio.openshell_connection` 建立网络连接，需要 await。后端 `.lifecycle` 提供异步生命周期；Docker/OpenShell 的 `.connection(resource)` 是无需 I/O 的普通方法；E2b 的 `.envd(resource)` 只读发现需要 await。envd 的 `.connection` / `.processes` 分别返回异步接口。同步入口的所有 I/O 都是阻塞调用。

后端对象保留原始 Engine/gateway/HTTP 客户端、凭据、作用域与超时，不重复转发生命周期方法。E2B 的 envd_endpoint、sandbox_domain、request_timeout 在 `e2b()` 配置，连接资源时无需再次提供 API key 或 endpoint。工厂不隐式 create/start/probe、不恢复暂停沙盒、不启动 daemon。公共参数校验和能力检查复用 Rust 客户端，Python 仅做语言参数、awaitable 和异常转换。

`aio` 的 Connection、Lifecycle、ProcessManager、Process、Output、Terminal 与 OutputSubscription 的所有 I/O 方法，以及网络工厂，都是显式的 `async def`。调用只创建 coroutine；执行 coroutine 时才进行参数转换、创建并 await 现有原生 Future。coroutine 可在 loop 启动前创建，也可直接传给 `asyncio.create_task()`；在首次执行前取消 Task 不发起 I/O。公开签名标注 await 后的 DTO 或 None，原生 `_native.pyi` 的内部 Future 桥接仍标注 Awaitable。配置工厂、get/connection 本地绑定、output/terminal 属性和 capabilities 不需要 await。

## 原生任务和输出资源

进程任务由实际的 pueued/envd/tmux 创建、持有和回收。OpenSOL 不托管 daemon，不增加执行队列或后台日志服务。`probe()` 做真实管理请求，返回 ManagerInfo；Pueue 可报告 CLI 版本，Tmux 可报告 client/server version，无法确认的版本为 None。`list()` 只返回原生当前保留的 ProcessInfo，不回传任务环境；Pueue 的 group 用于创建默认值和 list 过滤，不是权限边界。ProcessRef.pty 是可保存的 `bool | None` 模式提示，由适配器决定操作是否需要模式；envd List 返回 None，spawn 返回 True/False，Pueue 返回 False，Tmux 返回 True。envd 未知模式仍可 inspect、signal、kill、wait 和订阅，但 send_input、close_stdin、resize 返回 UnsupportedError，不发送猜测的 RPC；应保存 spawn 引用，或在已知模式时显式传 pty 参数。Pueue send 只需 task ID，重建的 `ProcessRef("pueue", id)` 仍可发送输入。

同步资源方法是阻塞调用；异步资源的相同 I/O 方法可 await，DTO 与异常相同。`get(ref)` 本地绑定任务，Process 与 OutputSubscription 的只读 `.ref` 均返回 ProcessRef，不做 I/O：

```python
from opensol import aio, OutputCursor

async def read_pages(config_file, pueue_directory):
    manager = aio.pueue(aio.local(), config_file=config_file,
                         pueue_directory=pueue_directory)
    await manager.probe()
    process, subscription = await manager.spawn(["python3", "-c", "print('resource')"])
    await process.wait(timeout=5)
    page = await process.output.read(max_bytes=4096)
    for chunk in page.chunks:
        print(chunk.channel, chunk.data)
    saved = page.after.value
    page = await process.output.read(cursor=OutputCursor(saved))
    previous = await process.output.read(cursor=page.before, direction="backward")
    tail = await process.output.tail(max_bytes=1024)
    print(page.at_tail, (await process.inspect()).state)
    await subscription.aclose()
```

Process 提供 inspect、wait、pause、start、restart、kill、remove、signal、send_input、close_stdin、resize，以及只读 output/terminal 视图。这些任务操作委托原生管理器，不缓存状态；订阅的 outcome 缓存仅属于原来的那次订阅。Pueue start 可强制启动 Queued/Stashed 并绕过队列限制和依赖；restart 仅用于终结任务，原地保留 ID、覆盖上次日志。remove 尊重原生状态/依赖，不暗中 kill；kill 回执不等于已经终结，仍需 inspect/wait。envd 只有原生 kill，pause/start/restart/remove 返回 Unsupported。

`Process.wait()` 和 `OutputSubscription.wait()` 返回共享的 `ProcessOutcome`，表示后端原生任务生命周期的完成，至少有 exit 或 native_result 一项可用；是否同时保证 OS 进程退出由具体后端决定。`outcome.exit` 是 `ExitStatus | None`，`outcome.native_result` 是脱敏的原生结果名称。Pueue Success/Failed 保留真实退出码，Killed、FailedToSpawn 等可以没有退出码；任务终态通过正常值返回。`ProcessInfo.outcome` 表达该原生生命周期的终态，非终态为 None；envd 通过原生完成事件提供含真实 ExitStatus 的 outcome。`Connection.exec()` 的 ExecResult 和 ExitStatus 保持原有含义。

`process.output` 表示带 channel 的原始 bytes，提供 `capabilities()`、`read(cursor=None, direction="forward", max_bytes=65536)`、`tail(max_bytes=65536)` 和 `subscribe()`。OutputPage 的只读属性是 chunks、before/after、at_head/at_tail；chunks.data 不做 UTF-8 解码。before/after 分别是本页之前/之后的边界：before 向后、after 向前读取相邻内容，页内始终保持原生顺序。无 cursor 时 forward 从保留头部、backward 从观测尾部开始；tail 取得最后一页。at_tail 与任务是否完成无关，at_head 无法确认时为 None；不能证明位置时 cursor 为 None。额外订阅调用原生重连，返回独立 OutputSubscription；页与订阅不推进对方的位置。

`process.terminal` 表示上游已渲染文本，提供 `capabilities()`、`read(cursor=None, direction="forward", lines=200)`、`tail(lines=200)`、`screen()`。TerminalPage 的只读属性是 lines、before/after、at_head/at_tail；TerminalScreen 是 cols、rows、lines。OutputCursor 和 TerminalCursor 均可用 value 保存、重建，但类型不可互换。Output 的 terminal channel 是 PTY 合并原始字节，可含控制序列，仍不等于 Terminal 文本；SDK 不增加终端解释器。

Output 大小须为 1–1,048,576 的整数，限制 chunks 合计字节数；Terminal 行数须为 1–10,000 的整数，方向只接受 forward/backward。参数在目标 I/O 前校验，查询在目标侧有界读取，立即返回当前数据，不下载全量再切片。视图构造、属性和 capabilities 只本地绑定，不探测或采集。同步方法阻塞并释放 GIL，aio 的属性同步返回视图，其 I/O 方法需要 await。

能力按表示分别查询：OutputCapabilities 有 read/tail/subscribe 和只描述订阅的 historical_output/streaming_output；TerminalCapabilities 有 read/tail/screen。Pueue 配置目录时支持 Output.read/tail；不论是否配置目录，订阅均是历史 true、实时 false。envd 只有 Output.subscribe 和 streaming_output 为 true。Tmux 支持 Output.subscribe，但只交付 Completed，历史/实时均 false；Terminal.tail/screen 为 true，read 为 false。subscribe=true 本身不承诺实时、历史或无丢失重连。

Pueue 需显式传 pueue_directory，指向注入 Connection 目标上的 daemon 数据目录，且与 config/profile 对应；要求 Linux 和 GNU stat/dd。Local、SSH、Docker 都经过同一 Connection，在目标侧同一打开的日志描述符上观察大小并计算正向、反向或尾部范围，不把远端路径当成本机路径。probe 和调用时检查依赖/权限，Output 的 read/tail 能力只表示配置，不缓存探测结果。未配置目录及 envd 的 Output.read/tail 返回 Unsupported。CLI 只能提供非结构化失败时保留脱敏 Backend；Connection 已确定的结构化错误种类不被改写。

cursor.value 是可保存的不透明字符串，绑定同一目标、任务、执行、表示和可观察的日志身份/大小，不包含凭据，也不选择目标路径。Queued/Stashed 尚未开始时返回空页，before/after 为相同的已知初始边界，不读取旧日志。Queued 的初始 cursor 可在同一排队执行开始后继续使用；无法确认同次执行的转换报告 Conflict。任务原地重启、可检测的文件替换/截断返回 Conflict，清理任务/日志返回 NotFound，非法格式或参数返回 InvalidArgument，不自动重置。文件身份/大小无法检测两次请求之间截断后重新写长的全部历史，也无法识别完整克隆目标；SDK 不归档或恢复上游已清除的数据。

## tmux 任务与终端文本

同步 `sdk.tmux` 和 `aio.tmux` 形状相同，构造不做 I/O。socket_path 必须为 Connection 目标上的绝对路径，session_prefix 为非空 ASCII 字母、数字、下划线或连字符。调用方安装 tmux >= 3.5，启动并保留 server；SDK 的 tmux 管理请求使用显式 socket 和 `-N`，不启动/关闭 server 或修改全局配置。probe 实际访问 server 并报告可确认的 client/server version。目标环境还需 `env` 和 `/usr/bin/nice`（支持 `-n 0 --`），任务环境不覆盖管理客户端环境。

```python
from opensol import aio

async def terminal_job(connection, socket_path):
    manager = aio.tmux(connection, socket_path=socket_path)
    process, subscription = await manager.spawn(["python3", "-c", "print('screen')"], pty=(80, 24))
    try:
        outcome = await process.wait(timeout=5)
        screen = await process.terminal.screen()  # TerminalScreen，cols / rows / lines。
        history = await process.terminal.tail(lines=200)  # 仅有限滚动历史。
        await process.remove()  # 仅 dead pane 可移除；运行中返回 ConflictError。
        return outcome, screen
    finally:
        await subscription.aclose()
```

每任务为独立 session/window/pane，ProcessRef.id 是可保存的不透明完整身份，pty=True。list 只发现配置前缀内带有效归属的任务；前缀不是安全隔离，调用方不得修改归属选项或重组被管理 session。spawn 必须传 `pty=(cols, rows)`，未传时在资源创建前返回 UnsupportedError。restart 仅对 dead pane 生效，保留任务身份、开始新代次；remove 在原生队列中检查 dead 条件，不暗中 kill。kill 销毁 pane、任务和画面，后续 inspect/wait/Terminal 查询返回 NotFoundError，不伪造退出结果。

指定 cwd 后，每次初始执行及 restart 都必须成功进入该目录才执行用户命令；失败时原生启动进程以 125 退出，OS 退出状态就绪后可从 `outcome.exit.code` 观察，用户命令不会在 tmux 的 HOME 或 `/` 回退目录执行。相对 cwd 经同一 Connection 目标的 `pwd -P` 解析并作为绝对路径保存在原生启动 argv 中；目标需要 `/bin/sh`，进入目录后再应用任务 env，保留非 POSIX 环境键、任务 PATH 和包含 `=` 的可执行路径。SDK 不缓存任务配置或通过目录预检查代替执行时的保证。

`process.terminal.screen()` 返回当前活动 grid 的原生渲染文本和真实 PTY 尺寸，恰好 rows 条物理行；tail 只捕获 `-S -<lines> -E -1` 的有界历史，零历史直接返回空页。使用 `capture-pane -p -N` 保留行尾空格，不合并软换行，不捕获属性转义、pending escape；仅按 LF 分隔，保留其余空白及 Unicode 数据，不按字符串字符数修整列宽。每次捕获在原生队列中核对完整身份、代次/PID、尺寸和历史信息，可观察冲突返回 ConflictError。

查询不读取 copy-mode 浏览画面。主屏与备用屏按原生活动 grid 选择，备用屏无历史时 tail 为空；退出备用屏后恢复主屏，不跨屏归档。清屏/清历史遵循原生配置，scroll-on-clear 可把清屏内容推入历史。Tmux 没有稳定历史游标：tail 的 before/after 为 None、at_tail 为 true，at_head 根据同次观测的全部保留历史判断；read 返回 UnsupportedError。SDK 不生成字节事件、pipe-pane 日志或后台采集，也不改变全局配置。

send_input 接受任意 bytes，经原生 `send-keys -H` 发送，不经过 paste buffer 或文件；PTY 行规仍由目标控制。为支持 8192 字节上限，一条受身份保护的原生命令经 `source-file -` 从 Connection stdin 传入；输入能力要求 Connection 支持 stdin（Local/SSH/Docker 均支持）。空 bytes 不产生按键；超限在目标 I/O 前返回 InvalidArgumentError，由调用方显式拆分，不自动重试不确定发送。创建会关闭本 pane 的原生退出横幅，避免首行被滚入历史；管理客户端的 `-u` 保留 UTF-8 文本。

Tmux 的 state、wait 和 Completed 均以原生 pane 生命周期为准，pane Dead/EOF 即可使 `ProcessInfo.state="exited"` 并返回完成。pane Dead/EOF、OS 进程退出和数值退出状态就绪可以分离；wait 返回后 OS 进程仍可能继续工作，`exit=None` 不能推断 OS 进程成功或已退出。可用的退出码和可确认的便携信号编号保留在 outcome.exit；其他信号保留 `SIG...` 名称，无可用 OS 状态时保留 Dead。

初始及额外订阅在建立时绑定原生执行代次和 PID，仅交付 Completed，不生成 Output 事件。可检测的内部或外部 respawn 返回 ConflictError，任务清理返回 NotFoundError。已完成的订阅始终保留首次结果，包括 `exit=None`，不会随 OS 状态就绪而补填；`Process.wait()` / `Process.inspect()` 查询当前执行，后续可取得已就绪的状态。关闭/取消订阅或 wait 不终止任务，server 消失后 SDK 不恢复进程。详见[后端契约](../../README.md#tmux)。

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
    task = asyncio.create_task(connection.exec(["printf", "%s", "hello"], timeout=5))
    result = await task
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
    process, subscription = native.processes.spawn(["python3", "-c", "print('managed')"])
    while (event := subscription.next_event(timeout=10)) is not None:
        if event.outcome is not None:
            break
        print(event.channel, event.data)
finally:
    lifecycle.delete(resource)
```

envd 的两种公共视图独立使用。`backend.envd(resource)` 只读发现连接配置，不隐式恢复暂停沙盒；aio 对应 `await backend.envd(resource)`。直接 `envd(endpoint, access_token=...)` 不需要 E2B 生命周期。

## 输出订阅与取消

`OutputSubscription.next_event(timeout=...)` 返回一条字节输出或完成事件；订阅结束后返回 None。正常输出事件 `.channel` 为 stdout/stderr/terminal/combined，`.data` 为 bytes，`.outcome` 为 None；完成事件 `.outcome` 是 ProcessOutcome，channel 为 None、data 为空 bytes。缺少完成证据的意外断流报告 TransportError。Pueue 在读取可用日志后交付完成事件；权限、传输、协议和日志读取失败仍返回 SDK 异常。

同步 `process, subscription = manager.spawn(...)` 和 aio 的 `process, subscription = await manager.spawn(...)` 均返回 `(Process, OutputSubscription)`。公共 manager 用原生引用本地构造任务对象，保留 envd Start 的原始流及 Pueue/Tmux 的初始执行标识，不增加 probe、状态查询或重连。两者的 `.ref` 表示同一任务；`manager.get(saved_ref)` 只绑定任务，不消费订阅。

`subscription.next_event()` 的等待超时只中止本次等待，保留订阅，可以继续读取。`subscription.wait(timeout=None)` 消费并丢弃剩余输出，返回并缓存这次订阅的 ProcessOutcome；需要日志时先逐条读取。有限 timeout 必须正数且有限，用单调时钟为整个调用计时，包括消费锁排队、每个原生事件和完成交付；持续输出不会重新计时。同步等待释放 GIL，超时为带 backend/operation 的 SDK TimeoutError，不关闭订阅、发信号或取消任务，之后可继续 wait/next_event。同一任务 ID 重启后，已有订阅的完成缓存仍属于原来的执行，Process.wait 则查询原生管理器。同步 close 释放订阅、唤醒阻塞读取，但不杀死进程。可保存 `.ref`，再用 `manager.get(saved_ref).output.subscribe()` 按原生能力重新订阅。每个订阅的事件读取串行化；并发消费应在应用层扇出。

Pueue 的初始与额外订阅在原生订阅建立时记录 created/enqueued/start 执行标识，复用分页输出的规则。同一 Queued 执行开始是合法转换，无法确认同次执行的 Stashed 转换、原地重启或 task ID 替换返回 ConflictError，原生任务清理返回 NotFoundError；即使一直未消费的旧订阅也不返回新执行的输出或结果。SDK 在消费前、wait 后和 follow 后核对，收齐并确认同次执行后才交付日志与完成事件；已交付的完成缓存不再查询当前任务。检查依赖原生可观测标识，不增加日志归档或后台监控，也不能恢复已经被原生覆盖的旧日志。aio 的绑定发生在 `await manager.spawn()` / `await process.output.subscribe()` 建立原生订阅时。

异步订阅使用 `await subscription.next_event(timeout=...)`、`await subscription.wait(timeout=...)` 和 `await subscription.aclose()`，并支持 `async for`。next_event 的 timeout 从该读取获得消费权后开始；wait 的总时限包括这段排队时间及 Rust awaitable。取消读取、取消 wait 或超时不会关闭订阅；Rust 已消费但 Python 尚未收到的事件或终结错误仍保留，下次读取保持顺序。wait 已消费并丢弃的输出不回滚。每个订阅至多保留一条待交付事件，不预取或后台分发。aclose 可重复调用并唤醒阻塞读取；它不发信号，不删除环境。

```python
from opensol import aio

async def watch(config_file):
    manager = aio.pueue(aio.local(), config_file=config_file)
    process, subscription = await manager.spawn(["python3", "-c", "print('managed')"])
    try:
        async for event in subscription:
            if event.outcome is not None:
                print("completed", event.outcome)
            else:
                print(event.channel, event.data)
    finally:
        await subscription.aclose()
    # 若需主动终止仍在运行的任务：await process.signal(15)
```

coroutine 开始执行后，取消 exec、create、spawn 等操作仅取消客户端等待，原生清理能力由后端决定；这不证明远端未执行或已经停止。SDK 不自动重试结果不确定的副作用操作。需要终止请显式 signal，需要销毁请显式 delete；丢弃客户端或订阅不会自动删除环境。

`process.wait(timeout=...)` 超时不代表进程已终止。明确停止请用 `process.kill()` 或支持的 signal；删除环境请显式 `Lifecycle.delete()`。包装对象没有自动销毁远端环境的上下文管理器。

Pueue 订阅完成后才返回合并日志，并要求 CLI 与 daemon 在同一执行环境、`client.read_local_logs: true`；独立资源查询可读取运行中的原生日志。它不支持 PTY 或关闭 stdin；输入限 UTF-8 且无 NUL，在 Connection I/O 前按 UnsupportedError 拒绝不符合的载荷。`binary_stdin=true` 保证任意 bytes，false 表示具体限制由适配器判断。envd 原样传递 bytes，可以实时输出和 PTY，但不承诺完整历史或 daemon 重启恢复。详见[能力矩阵](../../README.md#第三层的独立能力)。

## 错误与验证

`SdkError` 的具体子类为 InvalidArgumentError、UnsupportedError、NotFoundError、ConflictError、PermissionError、TimeoutError、TransportError、BackendError。它们带 `.backend`、`.operation`、`.kind`；参数转换阶段的 SDK 错误可以使用 `python` backend。Python 类型错误仍是普通 TypeError，而不是后端故障。

超时与断连都可能留下正在运行的远端操作；SDK 不自行重做不确定的副作用。不将异常字符串当成日志存档，原生输出通过正常返回值消费。

绑定验证由 SDK 根目录的 `Dockerfile.tests` / `scripts/check.sh` 执行，测试安装后的 wheel，而不导入未编译的源码。真实 Local/Pueue/Tmux/Docker 验证同步与异步进程、字节与资源边界；Tmux 验证渲染快照、8192 字节二进制输入、resize、代次绑定、取消和目标侧 socket/文件。本地 E2B HTTP fixture 验证资源包装和异常转换，envd ConnectRPC fixture 验证二进制 stdin/关闭、PTY 维度与终端字节及 resize。异步覆盖事件循环推进、读取/等待取消、输出及完成事件交付前取消、无退出码的终态、意外断流、显式关闭和网络工厂的真实 gRPC 握手。供应商 RPC/TLS 的固定 schema 契约另由 Rust fixture 验证，不调用真实云服务。测试脚本从固定的上游 schema 在临时目录生成 Python protobuf 类型，grpcio-tools 仅为测试依赖。
