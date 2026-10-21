# Python 集成

OpenSOL（Open Sandbox Operation Layer）是 Rust 异步核心与适配器的薄绑定，不依赖 Python Docker/E2B SDK，也不反向调用 `kapy.*`。要求 Python 3.12+；PyO3 使用 `abi3-py312`。主接口为 `from opensol import AsyncConnection, AsyncSandboxManager, AsyncProcessManager, AsyncBackend`，使用 `pyo3-async-runtimes` 接入 asyncio；同一模块中的无 Async 前缀类型提供释放 GIL 的阻塞视图。两者复用同一核心、DTO、异常与能力含义。

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

异步类型采用 `AsyncSandboxManager`、`AsyncConnection`、`AsyncProcessManager`，并返回 `AsyncSandbox`、`AsyncProcess`、`AsyncOutput`、`AsyncTerminal` 和 `AsyncOutputSubscription`。下文无前缀名称表示共同的业务职责；同步视图使用这些无前缀类型。

- `SandboxManager`：`create/get/list`。create 返回 Sandbox；get(SandboxRef) 本地绑定，list 返回 SandboxInfo 列表。单个 Sandbox 提供 ref、inspect/start/stop/delete/fork，fork 返回子 Sandbox。`SandboxRef(backend, id)` 可保存并重建，不包含凭据；SandboxInfo.sandbox 为该引用。
- `Connection`：`exec(argv, cwd=..., env=..., stdin=b"", timeout=...)`、显式 `.shell(script)`。返回 `ExecResult`，不返回后台任务 ID。
- `ProcessManager`：除构造、backend 和 capabilities 外，只提供 `probe/list/get/spawn`。`get(ProcessRef(backend, id, pty=None))` 本地绑定轻量 Process 资源，单任务操作经由该资源执行；存在性与权限由后续 I/O 检查。

每层都提供 `.backend` 和 `.capabilities()`，查询不做 I/O。`manager.capabilities()` 是配置下的能力上限；`process.capabilities()` 同步返回同一字典形状，按引用的已知模式裁剪。list 等管理器字段仍表达上限；查询不探测存在性、权限、运行状态或 stdin 是否仍可用，支持也不保证调用成功。公共任务操作使用同一份裁剪结果，在派发 I/O 前拒绝不支持的操作。能力字典除布尔项外，`ProcessManagerCapabilities.supported_signals` 是信号编号列表；`SandboxManagerCapabilities.stop_preserves_memory` 表达 stop 是否保留进程内存，E2B 为 true，Docker、Kubernetes 与 OpenShell 为 false。

## 统一构造与数据对象

`AsyncBackend(config, *, connection=None)` 构造可共享的访问上下文；
`AsyncSandboxManager(source)`、`AsyncConnection(source, *, target=None)`、
`AsyncProcessManager(source, *, target=None, connection=None)` 直接返回业务客户端。
source 接受 BackendConfig、AsyncBackend 或同步 Backend。传配置构造并持有一个独立上下文；
传上下文复用同一客户端、pool/channel、凭据与作用域，不按配置值全局缓存。
Pueue/Tmux 的 connection 必须显式注入，可以是异步或同步 Connection，已有 Backend 的依赖不可被后续构造覆盖。

与 [OpenDAL 的 AsyncOperator/Operator](https://opendal.apache.org/docs/bindings/python/getting-started/#blocking-vs-async) 相同，构造客户端是普通调用，只有 I/O 方法需要 await。构造和转换不发网络请求，也不需要运行中的 Python 事件循环：

```python
from opensol import AsyncConnection, LocalConfig

connection = AsyncConnection(LocalConfig())
result = await connection.exec(["printf", "hello"])

# 同步调用方可取阻塞视图，或直接使用 Connection(LocalConfig())。
blocking = connection.to_connection()
result = blocking.exec(["printf", "hello"])
connection = blocking.to_async_connection()
```

| 异步主接口 | 阻塞视图 | 转换方法 |
| --- | --- | --- |
| AsyncBackend | Backend | to_backend / to_async_backend |
| AsyncSandboxManager | SandboxManager | to_sandbox_manager / to_async_sandbox_manager |
| AsyncConnection | Connection | to_connection / to_async_connection |
| AsyncProcessManager | ProcessManager | to_process_manager / to_async_process_manager |

转换保留同一原生上下文和目标；阻塞方法驱动 Rust 异步核心，不运行 Python 事件循环。

| 配置类型 | 业务入口与绑定 |
| --- | --- |
| LocalConfig、SshConfig | Connection；配置已确定执行目标 |
| DockerConfig | SandboxManager；Connection 需要 SandboxRef target |
| E2bConfig | SandboxManager；Connection/ProcessManager 需要 SandboxRef target，经原生 envd |
| EnvdConfig | Connection、ProcessManager；显式独立 endpoint |
| AgentSandboxConfig | SandboxManager；不猜测执行地址 |
| OpenShellConfig | SandboxManager；Connection 需要 SandboxRef target |
| SandboxdConfig | Connection；显式 endpoint，不从沙盒身份猜测 |
| PueueConfig、TmuxConfig | ProcessManager；显式注入 Connection，路径属于该执行目标 |

构造只实例化客户端、绑定目标；协议握手及只读发现延迟到首次 I/O，不 create/start/probe、
不启动 daemon，也不保证统一就绪。E2B 的两个入口在同一上下文和 target 首次 I/O 时只读发现 envd，
并发请求共享发现及同一原生 envd 客户端和 HTTP pool，不恢复暂停沙盒。取消或失败的只读发现可在下一次 I/O 重试；不重试结果不确定的副作用。新 Backend 可重新发现访问参数。
缺少必需目标/依赖、冲突参数或错误引用在目标 I/O 前报告 InvalidArgument；
不支持的业务入口报告 Unsupported。SDK 不猜测备用执行方式。

配置只描述访问，不含创建模板。每次 `manager.create(spec)` 提供完整规格：
DockerSandboxSpec 的 image/command/env/working_dir/host_config，
E2bSandboxSpec 的 template/env/metadata/sandbox_timeout/auto_pause，
AgentSandboxSpec 的 pod_template/volume_claim_templates/service，
OpenShellSandboxSpec 的 workload_template 或 spec_protobuf。所有规格均可提供 name/labels。
Docker HostConfig 与 Kubernetes PodTemplate/PVC 是 dict/list，绑定内部编码；
OpenShell spec_protobuf 是固定上游原生 SandboxSpec 的 bytes，不另造策略模型。
本地校验类型、互斥项及公共参数，复杂供应商 schema 仍由原生服务校验。

配置与规格是具有明确字段和默认值的数据对象，构造与序列化不做 I/O。例如：

```python
import opensol as sdk

config = sdk.DockerConfig()
saved = config.to_json()
restored = sdk.BackendConfig.from_json(saved)
spec = sdk.DockerSandboxSpec("image", command=["sleep", "infinity"],
                             host_config={"Memory": 536870912}, labels={"project": "demo"})
restored_spec = sdk.SandboxSpec.from_dict(spec.to_dict())
```

to_dict 返回独立数据，配置序列化明确包含凭据，需要按原凭据的要求保存；repr 隐藏凭据。
Backend、业务 API、绑定对象及订阅是运行时对象。API 保留 Backend，Sandbox/Process 保留管理器，
Pueue/Tmux Backend 保留 Connection；丢弃局部变量不会使已绑定资源失效，也不产生公共 API 缓存环。
释放客户端不 delete 沙盒、kill 任务或关闭调用方 daemon。

同步 I/O 阻塞并释放 GIL。Async* 的所有 I/O 都是显式 `async def`；
调用只创建 coroutine，运行时才转换参数并 await 原生 Future，可直接传给 asyncio.create_task。
在首次执行前取消 Task 不发起 I/O。配置/规格构造、get、ref、output/terminal 属性和 capabilities 同步。
未发布的旧供应商工厂、中间容器、`.open()` 工厂和 `opensol.aio` 命名空间已移除，不提供兼容别名。

## 沙盒发现与绑定

```python
import opensol as sdk

backend = sdk.AsyncBackend(sdk.DockerConfig())
manager = sdk.AsyncSandboxManager(backend)
sandbox = await manager.create(sdk.DockerSandboxSpec("my-image", command=["sleep", "infinity"], labels={"project": "example"}))
try:
    saved = sandbox.ref
    rebound = manager.get(saved)
    await rebound.start()
    print((await rebound.inspect()).state)
    for info in await manager.list():
        if info.labels.get("project") == "example":
            print(info.name, (await manager.get(info.sandbox).inspect()).state)
finally:
    await sandbox.delete()
```

异步 create/list 和 AsyncSandbox 的 inspect/start/stop/delete/fork 需要 await，构造、get、ref 和 capabilities 保持同步。同步视图提供相同操作。list 能力通过 `manager.capabilities()["list"]` 声明；Docker 包含当前 Engine 的停止容器，agent-sandbox 限定 namespace，E2B 限定 API key 可见范围且包含 running/paused，OpenShell 限定 workspace。原生分页由适配器读取完毕后返回，创建规格不作为隐式列表过滤条件。SandboxRef 可保存、重建；执行入口通过 target=sandbox.ref 绑定。

SandboxInfo.name 是只读 `str | None`，labels 是只读属性，每次返回 `dict[str, str]` 副本。它们来自此次原生 list/inspect 响应，缺少 name 为 None，缺少标签为空字典；不缓存创建元数据，也不在 list 后逐项 inspect。Docker 读取原生 Name/Names、Config.Labels/Labels 并移除名称开头 `/`；agent-sandbox/OpenShell 读取 metadata.name/labels；E2B 将 metadata["name"] 映射为 name，全部 metadata（含 name 项）作为 labels。E2B create 依次合并本次规格的 metadata、labels，最后由显式 name 覆盖 name 项；名称不保证唯一。

SandboxRef/ProcessRef 仅在原管理器配置作用域有效，恢复时需相同目标、账号/凭据范围、endpoint、namespace/workspace、Connection 及相关配置。引用不含凭据或完整路由，backend 校验不能确认目标相同；模式提示也不是原生状态缓存。

## 沙盒操作返回时点

create/start/stop/delete/fork 成功共同承诺对应原生请求已成功返回，不是统一的就绪、OS 执行可用、状态收敛或资源消失屏障。create/fork 返回可绑定沙盒；get 和 Connection 构造只本地绑定，首次 I/O 才握手或只读发现。Docker create 后需显式 start；Kubernetes 创建 CR、修改 operatingMode 后仍需 controller 收敛，inspect 只信任当前 generation 的 observed conditions；OpenShell 可能继续 provisioning/starting/deleting；E2B 保留原生创建和内存 pause/resume。

使用现有 inspect 观察状态，将每次请求与休眠放在同一个整体 deadline 内：

```python
import asyncio
import opensol as sdk

async def start_then_delete(sandbox):
    try:
        async with asyncio.timeout(30):
            await sandbox.start()
            while True:
                info = await sandbox.inspect()
                if info.state == "running":
                    break
                if info.state in {"failed", "completed"}:
                    raise RuntimeError(f"sandbox {info.state}: {info.native_state}")
                await asyncio.sleep(0.2)
        # Running 是后端的运行/就绪观察值；exec 仍可能因竞争或端点不可达失败。
    finally:
        async with asyncio.timeout(30):
            await sandbox.delete()
            while True:
                try:
                    await sandbox.inspect()
                except sdk.NotFoundError:
                    break  # 仅 NotFound 确认消失；权限、传输等错误照常抛出。
                await asyncio.sleep(0.2)
```

Pending/Unknown 不当作就绪，Failed/Completed 立即报告。循环次数、单次请求 timeout 和同步循环检查时间都不能代替整体超时；同步原生 request timeout 只限制单次调用，循环时间检查无法中断正在进行的阻塞请求。[可运行的 Docker 异步示例](../../examples/async_sandbox.py) 展示创建、发现恢复、启动和删除；SDK 不增加 wait-ready 接口或通用异步操作对象。

## 原生任务和输出资源

进程任务由实际的 pueued/envd/tmux 创建、持有和回收。OpenSOL 不托管 daemon，不增加执行队列或后台日志服务。`probe()` 做真实管理请求，返回 ManagerInfo；Pueue 可报告 CLI 版本，Tmux 可报告 client/server version，无法确认的版本为 None。`list()` 只返回原生当前保留的 ProcessInfo，不回传任务环境；Pueue 的 group 用于创建默认值和 list 过滤，不是权限边界。ProcessRef.pty 是可保存的 `bool | None` 模式提示，由适配器决定操作是否需要模式；envd List 返回 None，spawn 返回 True/False，Pueue 返回 False，Tmux 返回 True。envd 未知模式仍可 inspect、signal、kill、wait 和订阅，但 send_input、close_stdin、resize 返回 UnsupportedError，不发送猜测的 RPC；应保存 spawn 引用，或在已知模式时显式传 pty 参数。Pueue send 只需 task ID，重建的 `ProcessRef("pueue", id)` 仍可发送输入。

envd 的 `process.capabilities()` 在 pty=None 时禁用 stdin/binary_stdin/close_stdin/pty/resize；False 时允许后端的 stdin/close_stdin，禁用 pty/resize；True 时允许 PTY 输入和 resize，禁用 close_stdin。spawn 引用的已知模式及其本地恢复保留这些能力。Pueue 未知模式不裁剪 stdin，仍校验 UTF-8/NUL；Tmux 输入与 PTY 能力需要完整引用及 pty=True，stdin 还受注入 Connection 的能力上限约束。

同步资源方法是阻塞调用；异步资源的相同 I/O 方法可 await，DTO 与异常相同。`get(ref)` 本地绑定任务，Process 的只读 `.ref` 返回 ProcessRef，不做 I/O；订阅不公开任务引用：

```python
import opensol as sdk
from opensol import OutputCursor, PueueConfig, LocalConfig

async def read_pages(config_file, pueue_directory):
    connection = sdk.AsyncConnection(LocalConfig())
    manager = sdk.AsyncProcessManager(PueueConfig(config_file=config_file,
                         pueue_directory=pueue_directory), connection=connection)
    await manager.probe()
    process = await manager.spawn(["python3", "-c", "print('resource')"])
    await process.wait(timeout=5)
    page = await process.output.read(max_bytes=4096)
    for chunk in page.chunks:
        print(chunk.channel, chunk.data)
    saved = page.after.value
    page = await process.output.read(cursor=OutputCursor(saved))
    previous = await process.output.read(cursor=page.before, direction="backward")
    tail = await process.output.tail(max_bytes=1024)
    print(page.at_tail, (await process.inspect()).state)
```

Process 提供 capabilities、inspect、wait、pause、start、restart、kill、remove、signal、send_input、close_stdin、resize，以及只读 output/terminal 视图。这些任务操作委托原生管理器，不缓存状态；订阅通过完成事件交付其绑定执行的结果。Pueue start 可强制启动 Queued/Stashed 并绕过队列限制和依赖；restart 仅用于终结任务，原地保留 ID、覆盖上次日志。remove 尊重原生状态/依赖，不暗中 kill；envd 只有原生 kill，pause/start/restart/remove 返回 Unsupported。

kill 的共同保证是原生控制请求成功，不统一保证 OS 已退出或任务仍保留。Pueue/envd 后续 inspect/wait 取决于原生任务保留及结果窗口；Tmux 使用 kill-pane 销毁任务与画面，后续 inspect/wait/subscribe 返回 NotFoundError，不合成退出结果。

`ProcessInfo.state="completed"` 仅表示后端确认受管任务完成。`Process.wait(timeout=...)` 和订阅的完成事件提供同一种 `ProcessOutcome`，至少有 exit 或 native_result 一项可用。`outcome.exit` 是 `ExitStatus | None`，只保存原生可证实的 OS 退出码/信号；缺少 exit 不能推断成功或 OS 已退出。`outcome.native_result` 是脱敏的原生结果名称。Pueue Success/Failed 保留真实退出码，Killed、FailedToSpawn 等可无 exit。`ProcessInfo.outcome` 在非终态为 None；envd End 保留真实退出码。`Connection.exec()` 的 ExecResult 和 ExitStatus 保持原有含义。

`process.output` 表示带 channel 的原始 bytes，提供 `capabilities()`、`read(cursor=None, direction="forward", max_bytes=65536)`、`tail(max_bytes=65536)` 和 `subscribe()`。OutputPage 的只读属性是 chunks、before/after、at_head/at_tail；chunks.data 不做 UTF-8 解码。before/after 分别是本页之前/之后的边界：before 向后、after 向前读取相邻内容，页内始终保持原生顺序。无 cursor 时 forward 从保留头部、backward 从观测尾部开始；tail 取得最后一页。at_tail 与任务是否完成无关，at_head 无法确认时为 None；不能证明位置时 cursor 为 None。额外订阅调用原生重连，返回独立 OutputSubscription；页与订阅不推进对方的位置。

`process.terminal` 表示上游已渲染文本，提供 `capabilities()`、`read(cursor=None, direction="forward", lines=200)`、`tail(lines=200)`、`screen()`。TerminalPage 的只读属性是 lines、before/after、at_head/at_tail；TerminalScreen 是 cols、rows、lines。OutputCursor 和 TerminalCursor 均可用 value 保存、重建，但类型不可互换。Output 的 terminal channel 是 PTY 合并原始字节，可含控制序列，仍不等于 Terminal 文本；SDK 不增加终端解释器。

Output 大小须为 1–1,048,576 的整数，限制 chunks 合计字节数；Terminal 行数须为 1–10,000 的整数，方向只接受 forward/backward。参数在目标 I/O 前校验，查询在目标侧有界读取，立即返回当前数据，不下载全量再切片。视图构造、属性和 capabilities 只本地绑定，不探测或采集。同步方法阻塞并释放 GIL，异步接口的属性同步返回视图，其 I/O 方法需要 await。

能力按表示分别查询：OutputCapabilities 有 read/tail/subscribe 和只描述订阅的 historical_output/streaming_output；TerminalCapabilities 有 read/tail/screen。Pueue 配置目录时支持 Output.read/tail；不论是否配置目录，订阅均是历史 true、实时 false。envd 只有 Output.subscribe 和 streaming_output 为 true。Tmux 支持 Output.subscribe，但只交付 Completed，历史/实时均 false；Terminal.tail/screen 为 true，read 为 false。subscribe=true 本身不承诺实时、历史或无丢失重连。

Pueue 需显式传 pueue_directory，指向注入 Connection 目标上的 daemon 数据目录，且与 config/profile 对应；要求 Linux 和 GNU stat/dd。Local、SSH、Docker 都经过同一 Connection，在目标侧同一打开的日志描述符上观察大小并计算正向、反向或尾部范围，不把远端路径当成本机路径。probe 和调用时检查依赖/权限，Output 的 read/tail 能力只表示配置，不缓存探测结果。未配置目录及 envd 的 Output.read/tail 返回 Unsupported。CLI 只能提供非结构化失败时保留脱敏 Backend；Connection 已确定的结构化错误种类不被改写。

cursor.value 是可保存的不透明字符串，绑定同一目标、任务、执行、表示和可观察的日志身份/大小，不包含凭据，也不选择目标路径。Queued/Stashed 尚未开始时返回空页，before/after 为相同的已知初始边界，不读取旧日志。Queued 的初始 cursor 可在同一排队执行开始后继续使用；无法确认同次执行的转换报告 Conflict。任务原地重启、可检测的文件替换/截断返回 Conflict，清理任务/日志返回 NotFound，非法格式或参数返回 InvalidArgument，不自动重置。文件身份/大小无法检测两次请求之间截断后重新写长的全部历史，也无法识别完整克隆目标；SDK 不归档或恢复上游已清除的数据。

## tmux 任务与终端文本

异步通过 AsyncProcessManager(TmuxConfig(...), connection=...) 构造，同步使用对应阻塞视图，构造不做目标 I/O。socket_path 必须为 Connection 目标上的绝对路径，session_prefix 为非空 ASCII 字母、数字、下划线或连字符。调用方安装 tmux >= 3.5，启动并保留 server；SDK 的 tmux 管理请求使用显式 socket 和 `-N`，不启动/关闭 server 或修改全局配置。probe 实际访问 server 并报告可确认的 client/server version。目标环境还需 `env` 和 `/usr/bin/nice`（支持 `-n 0 --`），任务环境不覆盖管理客户端环境。

```python
import opensol as sdk
from opensol import LocalConfig, PueueConfig, TmuxConfig

async def terminal_job(connection, socket_path):
    manager = sdk.AsyncProcessManager(TmuxConfig(socket_path=socket_path), connection=connection)
    process = await manager.spawn(["python3", "-c", "print('screen')"], pty=(80, 24))
    outcome = await process.wait(timeout=5)
    screen = await process.terminal.screen()  # TerminalScreen，cols / rows / lines。
    history = await process.terminal.tail(lines=200)  # 仅有限滚动历史。
    await process.remove()  # 仅 dead pane 可移除；运行中返回 ConflictError。
    return outcome, screen
```

每任务为独立 session/window/pane，ProcessRef.id 是可保存的不透明完整身份，pty=True。list 只发现配置前缀内带有效归属的任务；前缀不是安全隔离，调用方不得修改归属选项或重组被管理 session。spawn 必须传 `pty=(cols, rows)`，未传时在资源创建前返回 UnsupportedError。restart 仅对 dead pane 生效，保留任务身份、开始新代次；remove 在原生队列中检查 dead 条件，不暗中 kill。kill 销毁 pane、任务和画面，后续 inspect/wait/Terminal 查询返回 NotFoundError，不伪造退出结果。

指定 cwd 后，每次初始执行及 restart 都必须成功进入该目录才执行用户命令；失败时原生启动进程以 125 退出，OS 退出状态就绪后可从 `outcome.exit.code` 观察，用户命令不会在 tmux 的 HOME 或 `/` 回退目录执行。相对 cwd 经同一 Connection 目标的 `pwd -P` 解析并作为绝对路径保存在原生启动 argv 中；目标需要 `/bin/sh`，进入目录后再应用任务 env，保留非 POSIX 环境键、任务 PATH 和包含 `=` 的可执行路径。SDK 不缓存任务配置或通过目录预检查代替执行时的保证。

`process.terminal.screen()` 返回当前活动 grid 的原生渲染文本和真实 PTY 尺寸，恰好 rows 条物理行；tail 只捕获 `-S -<lines> -E -1` 的有界历史，零历史直接返回空页。使用 `capture-pane -p -N` 保留行尾空格，不合并软换行，不捕获属性转义、pending escape；仅按 LF 分隔，保留其余空白及 Unicode 数据，不按字符串字符数修整列宽。每次捕获在原生队列中核对完整身份、代次/PID、尺寸和历史信息，可观察冲突返回 ConflictError。

查询不读取 copy-mode 浏览画面。主屏与备用屏按原生活动 grid 选择，备用屏无历史时 tail 为空；退出备用屏后恢复主屏，不跨屏归档。清屏/清历史遵循原生配置，scroll-on-clear 可把清屏内容推入历史。Tmux 没有稳定历史游标：tail 的 before/after 为 None、at_tail 为 true，at_head 根据同次观测的全部保留历史判断；read 返回 UnsupportedError。SDK 不生成字节事件、pipe-pane 日志或后台采集，也不改变全局配置。

send_input 接受任意 bytes，经原生 `send-keys -H` 发送，不经过 paste buffer 或文件；PTY 行规仍由目标控制。为支持 8192 字节上限，一条受身份保护的原生命令经 `source-file -` 从 Connection stdin 传入；输入能力要求 Connection 支持 stdin（Local/SSH/Docker 均支持）。空 bytes 不产生按键；超限在目标 I/O 前返回 InvalidArgumentError，由调用方显式拆分，不自动重试不确定发送。创建会关闭本 pane 的原生退出横幅，避免首行被滚入历史；管理客户端的 `-u` 保留 UTF-8 文本。

Tmux 的 state、wait 和 Completed 均以原生 pane 生命周期为准，pane Dead/EOF 即可使 `ProcessInfo.state="completed"` 并返回完成。pane Dead/EOF、OS 进程退出和数值退出状态就绪可以分离；wait 返回后 OS 进程仍可能继续工作，`exit=None` 不能推断 OS 进程成功或已退出。可用的退出码和可确认的便携信号编号保留在 outcome.exit；其他信号保留 `SIG...` 名称，无可用 OS 状态时保留 Dead。

每次 subscribe 建立时绑定原生执行代次和 PID，仅交付 Completed，不生成 Output 事件。可检测的内部或外部 respawn 返回 ConflictError，任务清理返回 NotFoundError。完成事件只交付一次，之后订阅结束；已交付事件的结果（包括 `exit=None`）保持不变；`Process.wait()` / `Process.inspect()` 查询当前执行，后续可取得已就绪的状态。关闭/取消订阅或 wait 不终止任务，server 消失后 SDK 不恢复进程。详见[后端契约](../../README.md#tmux)。

## 完成式命令

```python
import opensol as sdk

connection = sdk.Connection(sdk.LocalConfig())
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
import opensol as sdk
from opensol import LocalConfig, PueueConfig, TmuxConfig

async def main():
    connection = sdk.AsyncConnection(LocalConfig())
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
backend = sdk.AsyncBackend(sdk.E2bConfig(key))
sandbox = await sdk.AsyncSandboxManager(backend).create(sdk.E2bSandboxSpec())
try:
    connection = sdk.AsyncConnection(backend, target=sandbox.ref)
    processes = sdk.AsyncProcessManager(backend, target=sandbox.ref)
    result = await connection.exec(["printf", "%s", "short command"], timeout=10)
    process = await processes.spawn(["python3", "-c", "print('managed')"])
    subscription = await process.output.subscribe()
    try:
        async for event in subscription:
            if event.outcome is not None:
                break
            print(event.channel, event.data)
    finally:
        await subscription.aclose()
finally:
    await sandbox.delete()
```

envd 的两个异步业务入口独立使用，直接构造 AsyncConnection 或 AsyncProcessManager；E2B target 的首次 I/O 只读发现端点，不隐式恢复暂停沙盒。直接 EnvdConfig(endpoint, access_token=...) 不需要 E2B 生命周期。E2bSandboxSpec.sandbox_timeout 是本次创建的初始生存时间，默认 300 秒；start/resume 和 fork 分别明确使用 300 秒，不继承创建规格，也不与 request_timeout 混用。

## 输出订阅与取消

`OutputSubscription.next_event(timeout=...)` 返回一条字节输出或完成事件；订阅结束后返回 None。正常输出事件 `.channel` 为 stdout/stderr/terminal/combined，`.data` 为 bytes，`.outcome` 为 None；完成事件 `.outcome` 是 ProcessOutcome，channel 为 None、data 为空 bytes。缺少完成证据的意外断流报告 TransportError。Pueue 在读取可用日志后交付完成事件；权限、传输、协议和日志读取失败仍返回 SDK 异常。

同步 `process = manager.spawn(...)` 返回 Process，异步 `process = await manager.spawn(...)` 返回 AsyncProcess。Pueue/Tmux 不保存 spawn 初始订阅，每次 subscribe 都调用原生订阅路径，绑定本次可观察的执行；spawn 对象、clone、已有 Output 视图及重新绑定对象遵循同一时点。envd 固定协议不支持 restart，保留必要 Start 原始流：首次 subscribe 接管，不建立替代 Connect，也不提前读取；Process/Output clone 和不同视图共享一次移交，并发接管至多一方取得 Start，其余走原生 Connect。Start/Connect 的历史覆盖各遵循上游保留窗口。初始流只保留到接管或最后一个持有它的 Process/Output 被丢弃，不预取、归档或后台采集。任务引用通过 `process.ref` 保存，订阅不公开 `.ref`；`manager.get(saved_ref)` 只绑定任务，其订阅走原生路径。

`subscription.next_event(timeout=...)` 的超时只中止本次读取，保留订阅，可以继续读取。只关心任务完成时调用 `process.wait(timeout=...)`；需要输出时消费订阅，从完成事件的 outcome 获取结果。完成事件交付后 next_event 返回 None，订阅不提供 wait 或完成结果查询接口。同步读取释放 GIL，close 释放订阅并唤醒阻塞读取，不杀死进程。可保存 `process.ref`，再用 `manager.get(saved_ref).output.subscribe()` 按原生能力重新订阅。每个订阅的事件读取串行化；并发消费由应用层扇出。

Pueue 每次 subscribe 建立时记录 created/enqueued/start 执行标识，复用分页输出的规则。同一 Queued 执行开始是合法转换，无法确认同次执行的 Stashed 转换、原地重启或 task ID 替换返回 ConflictError，原生任务清理返回 NotFoundError；未消费的旧订阅也不返回新执行的输出或结果。SDK 在消费前、wait 后和 follow 后核对，确认同次执行后才交付日志与完成事件；完成事件交付后订阅结束，不再查询当前任务。与 restart 并发的 subscribe 可以绑定建立期间观察到的执行，不提供跨进程原子快照；restart 完成后建立的新订阅必须观察新执行。检查不增加日志归档或后台监控，也不能恢复已覆盖的旧日志。异步接口的绑定发生在 `await process.output.subscribe()`，不在 `await manager.spawn()`。

异步订阅使用 `await subscription.next_event(timeout=...)` 和 `await subscription.aclose()`，并支持 `async for`。next_event 的 timeout 从该读取获得消费权后开始。取消读取或超时不会关闭订阅；Rust 已消费但 Python 尚未收到的事件或终结错误仍保留，下次读取保持顺序。每个订阅至多保留一条待交付事件，不预取或后台分发。aclose 可重复调用并唤醒阻塞读取；它不发信号，不删除环境。

```python
import opensol as sdk
from opensol import LocalConfig, PueueConfig, TmuxConfig

async def watch(config_file):
    connection = sdk.AsyncConnection(LocalConfig())
    manager = sdk.AsyncProcessManager(PueueConfig(config_file=config_file), connection=connection)
    process = await manager.spawn(["python3", "-c", "print('managed')"])
    subscription = await process.output.subscribe()
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

`process.wait(timeout=...)` 超时不代表进程已终止。明确停止请用 `process.kill()` 或支持的 signal；删除环境请显式 `sandbox.delete()`。包装对象没有自动销毁远端环境的上下文管理器。

Pueue 订阅完成后才返回合并日志，并要求 CLI 与 daemon 在同一执行环境、`client.read_local_logs: true`；独立资源查询可读取运行中的原生日志。它不支持 PTY 或关闭 stdin；输入限 UTF-8 且无 NUL，在 Connection I/O 前按 UnsupportedError 拒绝不符合的载荷。`binary_stdin=true` 保证任意 bytes，false 表示具体限制由适配器判断。envd 原样传递 bytes，可以实时输出和 PTY，但不承诺完整历史或 daemon 重启恢复。详见[能力矩阵](../../README.md#第三层的独立能力)。

## 错误与验证

`SdkError` 的具体子类为 InvalidArgumentError、UnsupportedError、NotFoundError、ConflictError、PermissionError、TimeoutError、TransportError、BackendError。它们带 `.backend`、`.operation`、`.kind`；参数转换阶段的 SDK 错误可以使用 `python` backend。配置、规格与目标绑定的类型错误在本地报告 InvalidArgumentError；其他 Python 调用签名错误仍是普通 TypeError。

超时与断连都可能留下正在运行的远端操作；SDK 不自行重做不确定的副作用。不将异常字符串当成日志存档，原生输出通过正常返回值消费。

绑定验证由 SDK 根目录的 `Dockerfile.tests` / `scripts/check.sh` 执行，测试安装后的 wheel，而不导入未编译的源码。真实 Local/Pueue/Tmux/Docker 验证同步与异步进程、字节与资源边界；Tmux 验证渲染快照、8192 字节二进制输入、resize、代次绑定、取消和目标侧 socket/文件。本地 E2B HTTP fixture 验证资源包装和异常转换，envd ConnectRPC fixture 验证二进制 stdin/关闭、PTY 维度与终端字节及 resize。异步覆盖事件循环推进、读取/等待取消、输出及完成事件交付前取消、无退出码的终态、意外断流、显式关闭、首次 I/O 的真实 gRPC 握手和同步/异步视图转换。供应商 RPC/TLS 的固定 schema 契约另由 Rust fixture 验证，不调用真实云服务。测试脚本从固定的上游 schema 在临时目录生成 Python protobuf 类型，grpcio-tools 仅为测试依赖。
