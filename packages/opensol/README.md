# OpenSOL

**Open Sandbox Operation Layer**：同一 Rust 核心提供 Python 同步入口 `opensol` 和原生异步入口 `opensol.aio`。当前版本尚未发布，原 `agent-sandbox-sdk` 包与 crate 名称不再保留别名；供应商 `agent-sandbox` 的 backend、feature 与协议名称保持原义。

面向 Agent 的可组合沙盒集成 SDK，以 Rust 为核心。借鉴 OpenDAL 的统一接口、独立后端、显式能力查询，但不实现沙盒运行时、守护进程、队列或控制面。

这是独立 Cargo workspace，不依赖 `kapy.*`，也不替换本仓库当前的进程实现或 shellctl。首版包含 Rust 和独立的 Python/PyO3 绑定。

## 三层不是流水线

三层表示责任边界，不是强制 `Lifecycle → Connection → ProcessManager` 的调用链：

| 公共接口 | 职责 | 返回或标识 |
| --- | --- | --- |
| `Lifecycle` | `create/inspect/start/stop/delete/fork` | 后端名与不透明资源 ID |
| `Connection` | `exec`，等待短命令完成 | 字节 stdout/stderr、退出状态 |
| `ProcessManager` | 原生任务发现与创建；绑定 `Process` 资源 | 后端原生进程 ID 与含初始订阅的 `ProcessHandle` |

例如：

```text
DockerLifecycle ──资源引用──> DockerConnection ──注入──> Pueue
E2bLifecycle ──资源引用──> Envd ──视图──> Connection + ProcessManager
AgentSandboxLifecycle                  Sandboxd (Connection)
OpenShellLifecycle ──资源引用──> OpenShellConnection
Local、Ssh 或 DockerConnection ──注入──> Pueue 或 Tmux
```

Pueue 和 Tmux 包装已有的公共 `Connection`；envd 通过原生 RPC 同时实现第二、第三层，`connection()` / `processes()` 返回复用同一原生客户端的公共视图。生命周期层也不负责为任意资源猜测网络地址。`sandboxd` 的端点由调用方提供。

[`opensol-core`](crates/core/src/lib.rs) 区分适配器扩展契约与业务客户端，共享 DTO、错误和能力模型：

| 适配器实现 | 业务使用 |
| --- | --- |
| `raw::LifecycleAccess` | `Lifecycle` |
| `raw::ExecAccess` | `Connection` |
| `raw::ProcessAccess` | `ProcessManager`、绑定的 `Process` / `ProcessOutput` |

业务客户端是可廉价 clone 的具体类型，通过 `Lifecycle::new(adapter)` 等构造；不要求调用方管理动态 trait 对象。公共入口统一检查参数、引用与已声明能力，适配器负责原生协议、状态、权限和保留策略。第三方后端只需实现对应 raw trait 并包装，无需插件注册、全局状态、配置 DSL 或总控 `Sandbox`。这借鉴 OpenDAL 的 raw Service / 公共 Operator 边界，三类能力仍独立组合。

## 集成与能力

| 后端 / Cargo feature | 生命周期 | 短命令连接 | 进程管理 |
| --- | --- | --- | --- |
| Local / `local` | 无 | 本机 argv 与显式 shell | 可组合本机 Pueue / Tmux；没有隔离保证 |
| SSH / `ssh` | 无 | 系统 OpenSSH | 可组合远端 Pueue / Tmux |
| Docker / `docker` | create/inspect/start/stop/delete | 原生 Engine exec，Bollard | 可组合容器内 Pueue / Tmux |
| E2B / `e2b` | create/inspect/resume/pause/delete/native fork | 组合 envd | 组合 envd |
| agent-sandbox / `agent-sandbox` | Kubernetes Sandbox CR 生命周期 | 独立组合 sandboxd 或其他连接 | 无 SDK 自建进程服务 |
| OpenShell / `openshell` | 原生 gRPC 生命周期 | 原生 gRPC exec | 不提供通用进程管理 |
| Pueue / `pueue` | 无 | 复用注入的连接 | 官方 v4 CLI / pueued |
| Tmux / `tmux` | 无 | 复用注入的连接 | 已有 tmux >= 3.5 server，每任务一个 session/window/pane |
| envd / `envd` | 无 | 原生 Start 并收集结果 | 原生 ConnectRPC 进程接口 |
| sandboxd / `sandboxd` | 无 | 原生 gRPC Execute | 不提供连接外的进程句柄 |

Docker、agent-sandbox、OpenShell 的 `fork` 返回 `Unsupported`，不以重建、复制文件或 Docker commit 冒充内存 fork。E2B 使用原生 `fork`，一次返回一个子沙盒。

### 第三层的独立能力

| 能力 | Pueue | envd | Tmux |
| --- | --- | --- | --- |
| stdin | UTF-8、无 NUL 的有限输入 | 字节 stdin / PTY 输入 | 任意字节，单次最多 8192 字节；PTY 行规仍生效 |
| close_stdin | 不支持 | 非 PTY 支持 | 不支持 |
| PTY / resize | 不支持 | 支持；输出是 `terminal` 合并终端流 | 必须显式请求 PTY；支持 resize |
| 信号 | SIGINT / SIGTERM / SIGKILL，由原生任务状态决定 | SIGTERM / SIGKILL；SIGINT 返回 Unsupported | 不支持 |
| 脱离输出订阅继续运行 | 支持 | 支持 | 支持 |
| 按 ID 重连 | 原生任务仍保留时 | 原生 PID 仍可连接时 | 原生 session、window、pane 和归属仍匹配时 |
| 历史输出 | Pueue 仍保留任务及日志时 | 不承诺完整历史输出 | 不支持 |
| 实时输出 | 首版不提供，完成后返回合并日志 | 原生输出流 | 不支持；订阅只交付 Completed |
| daemon 重启后恢复运行进程 | 不承诺 | 不承诺 | 不承诺 |
| probe / list | 原生 status；list 可按配置 group 过滤 | 原生 List | 实际访问 server；只发现本前缀内有效归属的任务 |
| pause / start | 原生暂停及开始；start 可强制启动排队任务 | 不支持 | 不支持 |
| restart / remove | 原生原地重启、任务及日志移除 | 不支持 | 仅 dead pane；原生队列内检查，运行中 Conflict |
| kill | 原生单任务 kill | 原生 SIGKILL | 销毁 pane 和任务；后续 NotFound，没有退出结果 |
| 有限字节输出查询 | 显式配置 `pueue_directory` 后支持 | 不支持 | 不支持 |
| terminal_snapshot | 不支持 | 不支持 | 当前渲染屏幕的原生文本字节，无滚动历史或属性转义 |

运行中、可重连、日志可回读、daemon 重启恢复是不同能力。对具体实例调用 `capabilities()`；不要仅根据后端名假定所有操作可用。`supported_signals` 返回明确的信号集合，Python 对应编号列表；生命周期的 `stop_preserves_memory` 仅在 E2B pause 的原生保证下为 true，Docker stop、Kubernetes Suspended 和 OpenShell 为 false。

`binary_stdin=true` 保证任意字节输入；请求大小限制仍由适配器检查。false 表示存在由适配器检查的字节内容限制。Pueue 在 Connection I/O 前拒绝无效 UTF-8 和 NUL，返回 Unsupported；envd 原样传递 bytes；Tmux 将每个字节经 `send-keys -H` 发送，超过 8192 字节在目标 I/O 前返回 InvalidArgument，由调用方显式拆分。`ProcessRef.pty` 是可保存的可选模式提示，由实际操作的适配器决定是否需要；Pueue send 只需 task ID，envd 输入路由及终端控制需要已知模式。

## 命令与错误契约

- 默认传显式 argv，只有 `Command::shell` / Python `.shell()` 使用 `/bin/sh -c`。SSH 和 Pueue 所需的 POSIX shell 转换逐项引用参数，不直接拼接用户命令。
- SSH 远端和 Pueue 的 CLI/daemon 执行环境需要 `env`，以及支持 `-n 0 --` 的 `/usr/bin/nice`。该固定原生 helper 在环境覆盖后保持优先级不变，并保留包含 `=` 的可执行路径和非 POSIX 环境键；任务 PATH 不影响 helper 定位。
- `cwd`、`env` 是请求字段，`stdin`、stdout、stderr 都使用字节。PTY、Pueue 合并输出分别标为 `Terminal`、`Combined`，不伪装成分离 stderr。
- 第二层 `exec` 等待完成，不返回后台任务 ID。Rust 为 async I/O，Python 根入口为释放 GIL 的阻塞调用，`opensol.aio` 将同一 Rust Future 接入调用方的 asyncio loop；输出收集到内存，不适合无限输出。
- 非零退出码属于正常 `ExecResult`，不是传输异常。信号退出可以没有数字退出码。管理任务的 wait 按后端原生任务生命周期确认完成，返回 `ProcessOutcome { exit: Option<ExitStatus>, native_result: Option<String> }`，至少一项可用；是否同时保证 OS 进程退出由具体后端决定。`ProcessInfo.outcome` 在该原生生命周期的非终态为 None，事件为 `Completed(ProcessOutcome)`。`Connection.exec` 继续返回带 ExitStatus 的 ExecResult。
- SDK 错误分为 InvalidArgument、Unsupported、NotFound、Conflict、Permission、Timeout、Transport、Backend，并带 backend/operation。普通错误不回显供应商响应正文、命令输入或凭据。
- `ExecRequest.timeout=None` 表示不设置整条命令的 SDK deadline；连接建立和原生控制请求仍可以有适配器级超时。零超时无效。
- 公共客户端在进入适配器前检查 argv、工作目录和环境变量的 NUL 等约束、正超时、PTY 尺寸、输出 limit 和引用后端，并按 capabilities 拒绝不支持的 stdin、PTY、信号和可选操作。输入载荷、模式信息的必要性、状态竞争和 cursor 版本由适配器判断。
- 超时/断连不等于远端进程已停止。Local 在取消时清理 Unix 进程组；SSH 只保证结束本机客户端；Docker 关闭 exec attachment，不保证终止 exec；envd 关闭订阅而不隐式发送 kill；OpenShell 的原生执行超时由服务端处理；sandboxd 取消请求的清理行为由服务端决定。
- SDK 不自行重试 create/fork/exec/spawn 等不确定结果。HTTP 客户端禁用重试及重定向；kubectl 仍保留 client-go 对明确 `Retry-After` 响应的内置行为，创建始终使用明确的 `metadata.name`，同次重放不会产生另一个名称的 CR。调用方不得盲目重做超时的副作用操作。
- 资源始终显式 `delete`。丢弃 Rust/Python 客户端、输出订阅或网络连接不自动销毁沙盒。

## 后端前提和边界

### Local / SSH / Docker

Local 在本机继承环境，并应用请求的环境覆盖；它不是沙盒。SSH 需要 POSIX 远端及系统 `ssh`，使用 BatchMode，保留用户配置、SSH agent 和 known_hosts 校验，不添加跳过主机校验的选项。OpenSSH 的退出 255 无法与远端命令退出 255 完全区分，统一报告 Transport；需要无此歧义的协议时使用其他连接。

Docker 使用调用者提供的 Engine 客户端，或 Bollard 的默认连接配置。不会自动 pull 镜像。create 返回停止的容器，start 需要显式调用；stop/start 不保留进程内存；delete 强制移除容器，但不会代替调用方清理外部卷。隔离、资源限额、挂载和网络设置来自 `DockerConfig.host_config`，SDK 不为它们作安全兜底。

`DockerLifecycle.lifecycle()` 与 `.connection(resource)` 分别提供公共生命周期和执行客户端，复用原来的 Engine 连接。绑定不检查存在性、不启动容器。独立 `DockerConnection::connect_defaults` 用于连接已有容器，使用时包装为公共 `Connection`。

Docker exec 观察原生完成后取消未被消费的 stdin，仍读完输出再返回退出码；进程仍在运行时的输入/传输错误不会被忽略。

### Pueue

调用方安装 Pueue v4，并管理 pueued、分组和并发策略。SDK 不安装、启动或关闭守护进程。CLI 必须与 daemon 位于相同执行环境，并设置 `client.read_local_logs: true`，才能通过 `pueue follow` 保留任意字节；原生远程日志协议会进行文本解码。

输出订阅在任务完成后读取合并日志，不宣称实时 streaming。独立的输出资源可以读取运行中已写入的原生日志：配置 `pueue_directory` 为同一执行目标上的 daemon 数据目录，要求 Linux 和 GNU stat/dd。所有文件访问经过注入的 Local/SSH/Docker Connection；不把远端路径当成本机路径。目录需与 config/profile 匹配，不自动猜测配置。`send_input` 仅支持 CLI 可表达的 UTF-8、无 NUL 输入，不提供关闭 stdin。清理原生任务或日志后，SDK 没有自己的副本；daemon 重启后的原生任务保留不等于进程恢复。

`probe()` 通过实际管理请求验证可访问性，配置日志访问时也检查目标依赖与目录。返回可确认的 CLI 版本，server_version 为 None；Pueue CLI 无法结构化区分的认证/连接失败保留为脱敏 Backend，注入 Connection 的结构化错误类别仍保留。静态能力表达配置，不缓存探测结果。

`pause/start/restart/kill/remove` 始终针对明确的单个 task ID。start 使用 Pueue 的原生强制开始语义，可绕过队列限制和依赖；restart 只允许终结任务，使用 `restart --in-place` 保留 ID 并覆盖上次日志。remove 由原生状态和依赖决定是否允许，绝不暗中 kill 或删除其他任务。kill 的原生回执表示接受请求，终结由 inspect/wait 确认。`ProcessOutcome.native_result` 只返回 Success、Failed、FailedToSpawn、Killed、Errored、DependencyFailed 等脱敏的原生名称；Success/Failed 保留真实退出码，Killed 等结果的 exit 为 None。订阅读取可用日志后交付 Completed；日志读取、权限、传输和协议失败仍按实际错误返回。

任务 `env` 在安全引用的队列命令内应用，不修改 Pueue CLI 的 PATH、HOME 或配置环境。

### Tmux

调用方安装 tmux 3.5 或更新版本并启动 server，保留一个 session 或自行配置 `exit-empty off`。适配器通过注入的 Connection 执行所有目标操作；tmux 管理请求始终显式指定 `-S socket_path` 和 `-N`，不隐式启动、关闭 server，不修改全局选项。`probe()` 实际访问 server，并返回可确认的 client/server version；离线、权限或版本不支持均返回真实类别的脱敏错误。Rust 使用 `Tmux::new(connection, TmuxConfig::new("/absolute/target/socket"))`，再包装为 `ProcessManager`；独立 `tmux` feature 不要求 local/ssh/docker。配置可选 program、session_prefix 和正数 request_timeout，构造不进行目标 I/O。

每个任务占用独立 session/window/pane，原生 user option 保存归属与执行代次。ProcessRef 是可保存的不透明完整身份，pty 为 True；list 只发现本前缀且归属有效的任务，不接管用户 session。前缀限制发现范围，不是权限隔离。调用方不得改写归属选项或重组被管理 session。

spawn 必须显式提供 PTY 尺寸；缺少 PTY 在创建任何资源前返回 Unsupported。创建在一个原生队列中先启动阻塞于 shell 内建 read 的占位 pane，配置本任务的 remain-on-exit、归属与尺寸，再以初始化专用的强制 respawn 启动实际命令。本 pane 的 remain-on-exit-format 为空，避免默认退出横幅把首行滚入历史；管理客户端显式使用 `-u`，保留 UTF-8 文本和结构化分隔字段。目标需要 `/bin/sh`、`env` 与支持 `-n 0 --` 的 `/usr/bin/nice`；任务 env 只应用于被管理命令，不污染 tmux 客户端。

指定 cwd 时，完整启动 argv 包含进入目录后才 exec 用户 argv 的固定 shell 门槛，任务环境在成功进入目录后应用，仍保留非 POSIX 环境键及包含 `=` 的可执行路径。相对 cwd 使用同一 Connection 目标的 `pwd -P` 解析为绝对路径，保留到后续执行。tmux 原生 chdir 失败会回退 HOME 或 `/`，因此每次初始执行和 restart 都重新进入该绝对目录，失败时实际启动进程以 125 退出，用户命令不会在回退目录执行。OS 退出状态就绪后可由 wait/inspect 读取该结果，不是 spawn 的同步目录预检查。tmux 保存完整启动 argv/cwd/env，后续 restart 不由 SDK 保存或重建任务配置。

state、wait 和 Completed 均以原生 pane 生命周期为准：pane Dead/EOF 时映射 Exited 并可返回完成。pane Dead/EOF、OS 进程退出和数值退出状态就绪可以分离；wait 返回后 OS 进程仍可能继续工作，exit=None 不能推断 OS 进程成功或已退出。wait 轮询原生 pane 状态，超时仅停止等待。可用的真实退出码、原生数字信号以及可确认的 INT/KILL/TERM 名称对应编号保留在 exit，其他原生信号以 `SIG...` 名称保留在 native_result，无可用 OS 状态时仅报告 `Dead`。

restart 只允许 dead pane，保留引用并更新原生执行代次；remove 在同一原生队列内按 dead 条件销毁任务，运行中返回 Conflict。kill 使用 kill-pane，销毁任务和画面，后续查询为 NotFound，不生成退出结果。server 消失返回连接错误，SDK 不恢复进程。

初始与额外订阅在建立时绑定执行代次与原生 pane PID，只交付 Completed，不生成 Output 事件。消费与完成交付前核对代次；可检测的内部/外部 respawn 返回 Conflict，原生清理返回 NotFound。已完成的 Python handle 始终保留首次结果，包括 exit=None，不会随 OS 状态就绪而补填；Process.wait/inspect 查询当前执行，后续可取得已就绪的状态。不承诺检测两次观察间无法分辨的外部修改。轮询没有独立后台任务，关闭或取消订阅不会 kill。

输入直接使用 `send-keys -H`，无 UTF-8 或 NUL 限制，不使用 paste buffer 或临时文件。为绕过 tmux 3.5 的 CLI 消息容量限制，一条经过完整身份保护的发送命令由原生 `source-file -` 从 stdin 读取，仍仅执行一次 send-keys；输入能力要求注入 Connection 支持 stdin，Local/SSH/Docker 均支持。空输入不发送按键；单次上限 8192 字节，超过上限在目标 I/O 前返回 InvalidArgument。PTY 行规仍控制目标如何接收字节，应用若需逐字节接收应自行切换 raw 模式。不会把输入解释为额外的 signal 操作，也不会重试结果不确定的输入。

```python
# server 已由调用方启动；socket 路径属于 connection 的目标机器。
manager = sdk.tmux(sdk.local(), socket_path="/tmp/my-tmux.sock")
handle = manager.spawn(["python3", "-c", "print('terminal')"], pty=(80, 24))
process = manager.get(handle.process)
print(process.wait(timeout=5).exit)
screen = process.output.snapshot()  # bytes：当前渲染画面，没有 cursor。
process.remove()
```

`terminal_snapshot=true` 与 output_resource/historical_output/streaming_output 独立。snapshot 使用原生 `capture-pane -p`，返回当前画面的文本字节，不主动读取滚动历史或属性转义。它受尺寸、换行、清屏和重绘影响，不是原始 stdout/stderr 或可追加日志；stat/read 仍为 Unsupported。Pueue/envd 的快照能力默认为 false。

### E2B / envd

E2B 控制面使用 v2 create/connect、inspect、full-memory pause、delete 和原生 fork。create 一般返回运行中沙盒；stop 是原生内存暂停，start 是 resume，不等同 Docker stop/start。权限、模板与套餐是否允许操作仍由服务端决定。

`E2bLifecycle.envd(resource)` 只读 inspect 发现 envd token/domain，复用原 HTTP pool 和超时配置构造 envd 客户端，不隐式 resume。也可以直接构造 `EnvdConfig` 连接独立 envd。API key 与 envd access token 分开，按请求设置；E2B 代理路由通过 Sandbox ID/port headers 传递。

envd 的 `Start` / `Connect` 是 ConnectRPC，而不是假设为 gRPC。字节输出、stdin、信号和 PTY 使用固定的上游 protobuf schema。`status` 使用原生 List；进程不在 List 中时返回 NotFound，不能推断历史退出码。原生 End 映射为带真实 ExitStatus 的 Completed outcome；缺少完成证据的意外断流返回 Transport。重连遵循 envd 的保留窗口，不是持久日志系统。

`probe/list` 同样使用原生 List，kill 使用原生 SendSignal(SIGKILL)。固定协议没有暂停、开始、重启、历史移除和分页日志 RPC，对应操作返回 Unsupported。`ProcessRef.pty` 为 `Option<bool>`：spawn 返回已知模式，List 不包含 PTY 元信息，返回 `None`。未知模式仍可 inspect、signal、kill、wait 和订阅；send_input、close_stdin、resize 显式返回 Unsupported，不猜测 RPC 通道。应用应保存 spawn 引用，或在已知模式时显式构造引用。Pueue 返回 `Some(false)`。envd 不报告客户端/服务端版本，ManagerInfo 的两项版本为 None。

短命令结束后，SendInput/CloseStdin 可能因原生进程已移除而返回 NotFound；只有同时取得含真实 ExitStatus 的完成事件才将其视为正常完成。等待这类竞争的完成事件受控制请求超时限制，其他输入错误仍然报错。

### Kubernetes SIGs agent-sandbox / sandboxd

生命周期通过系统 kubectl 操作 `agents.x-k8s.io/v1beta1` 的 Sandbox CR，复用 kubeconfig/context/exec credential provider。PodTemplate 与 PVC 使用供应商原生 JSON 配置，交由 Kubernetes 校验。start/stop 只修改 `spec.operatingMode`，不等待 controller 完成；inspect 使用当前 generation 的 observed conditions，不能把 desired mode 当作实际状态。

Suspended 删除 Pod、保留 CR 和卷，不保留内存。sandboxd 是独立原生 gRPC Connection，不从旧 HTTP router 猜地址。其 Execute 没有 stdin 字段，非空 stdin 返回 Unsupported；连接绑定的 Start 缺少脱离/重连语义，因此首版不冒充第三层。

### NVIDIA OpenShell

使用 OpenShell v0.1.4 原生 gRPC，显式提供 gateway endpoint、workspace，以及可选的 bearer、CA、mTLS 证书/私钥。HTTPS 验证服务器证书，不读取 CLI 登录态，不自动建立代理隧道。远程网络上的明文 HTTP 不适合传凭据。

创建可以使用原生 `SandboxSpec` 或命名 workload template，二选一。exec 先只读查询 owner，转发路由 hint，然后调用原生 exec；不在不确定失败后重试。执行使用非 login shell 模式，保留 argv 和分离的字节输出。普通 exec 不等于主进程 connect：主进程的有限 replay 不作为任意进程历史接口。delete 表示服务端接受请求，不保证资源已同步消失。

`OpenShellLifecycle.lifecycle()` / `.connection(resource)` 复用同一 gateway channel、TLS、凭据和 workspace；connection 绑定无需 I/O。E2B、Docker、OpenShell 的后端专属连接入口保留在后端对象上，不加入 raw 生命周期契约。

## Rust 使用

从本目录执行；要求 Rust 1.89+，验证使用 Rust 1.99。核心不依赖任何供应商 SDK，适配器默认只开启 `local`，使用 feature 按需启用：

```toml
[dependencies]
opensol-core = { path = "../opensol/crates/core" }
opensol-adapters = { path = "../opensol/crates/adapters", default-features = false, features = ["docker", "pueue"] }
```

可编译的组合示例在 [`examples/`](examples/)：

```sh
cargo run -p opensol-adapters --example local_exec
# 需要已存在的镜像、Docker daemon，以及镜像内可作为主命令运行的 pueued。
PUEUE_IMAGE=your-pueue-image cargo run -p opensol-adapters --features docker,pueue --example docker_pueue
# 创建并显式删除真实的计费资源，仅在准备好云凭据后运行。
E2B_API_KEY=... cargo run -p opensol-adapters --features e2b --example e2b_envd
```

## Python 使用

构建与使用详见 [`bindings/python/README.md`](bindings/python/README.md)。示例只使用本机，不创建服务：

```python
import opensol as sdk

result = sdk.local().exec(["printf", "%s", "hello\n"], timeout=5)
assert result.success
assert result.stdout == b"hello\n"

# pueued 由调用方预先启动，SDK 只连接它。
processes = sdk.pueue(sdk.local(), config_file="/path/to/pueue.yml")
handle = processes.spawn(["python3", "-c", "print('job')"])
while (event := handle.next_event(timeout=10)) is not None:
    if event.outcome is not None:
        break
    print(event.channel, event.data)
```

`docker()`、`e2b()`、`openshell()` 返回保留配置的后端对象，通过 `.lifecycle` 操作资源。Docker/OpenShell 的 `.connection(resource)` 本地绑定公共 Connection；E2B 的 `.envd(resource)` 只读发现连接信息，复用原 API key、endpoint 和超时。同步与 aio 使用相同形状，aio 的 E2B 发现需要 await。独立端点工厂仍可连接已有资源。

aio 的公共 I/O 方法与网络工厂都是 `async def`：调用只创建 coroutine，执行 coroutine 时才创建并 await 原生 Future，可直接交给 `asyncio.create_task()`。配置型工厂、本地绑定与属性仍同步；连接或端点发现型工厂需要 await：

```python
import asyncio
from opensol import aio

async def main():
    result = await aio.local().exec(["printf", "%s", "hello\n"], timeout=5)
    print(result.stdout)
    # pueued 和配置文件由调用方提供。
    processes = aio.pueue(aio.local(), config_file="/path/to/pueue.yml")
    handle = await processes.spawn(["python3", "-c", "print('job')"])
    try:
        async for event in handle:
            if event.outcome is None:
                print(event.channel, event.data)
    finally:
        await handle.aclose()  # 只关闭订阅；终止进程需显式 signal。

asyncio.run(main())
```

取消 `next_event` 或等待超时后可继续读取，已从 Rust 消费但尚未交付 Python 的一条事件会保留。`wait` 会丢弃输出；`aclose` 可重复调用并唤醒阻塞读取，不终止任务。取消 create/spawn/exec 不表示远端未执行或已停止，也不触发自动重试。详见 [Python 订阅与取消](bindings/python/README.md#输出订阅与取消)；[本地异步示例](examples/async_local.py) 可直接运行。

## 进程与输出资源

公共 ProcessManager 只负责构造、后端信息、capabilities、probe、list、spawn、get。单任务操作先 `manager.get(ref)`，再使用绑定资源：

| 对象 | 公共入口 |
| --- | --- |
| `Process` | 引用与 output、inspect、wait、pause、start、restart、kill、remove、signal、send_input、close_stdin、resize |
| `ProcessOutput` | snapshot、stat、read、subscribe |
| `ProcessHandle` | 一次输出订阅及其 ProcessRef；Rust 使用 events / wait_events，Python 使用 next_event / wait / disconnect 或 aclose |

`Process` 绑定公共 ProcessManager 和 ProcessRef，构造与丢弃都不做远端操作，也不缓存权威状态。Rust/Python 均调用 `manager.get(ref)`；Rust 也可以使用 `Process::new(manager, ref)`。spawn 返回带初始输出订阅的 ProcessHandle，尤其保留 envd Start 的原始流。绑定 Process 不消费该订阅。

```python
manager = sdk.pueue(sdk.local(), config_file="/path/to/pueue.yml",
                    pueue_directory="/path/to/pueue-data")
print(manager.probe().client_version)
handle = manager.spawn(["python3", "-c", "print('managed')"])
process = manager.get(handle.process)
page = process.output.read(limit=65536)
for chunk in page.chunks:
    print(chunk.channel, chunk.data)
saved_cursor = page.next_cursor.value  # 可由应用保存
next_page = process.output.read(cursor=sdk.OutputCursor(saved_cursor))
print(process.inspect().state, next_page.at_end)
```

`process.output.stat/read` 查询绑定任务的原生日志；`process.output.subscribe()` 是建立额外订阅的唯一公共入口，检查 reconnect 能力并调用原生重连，返回独立 ProcessHandle。订阅不要求 output_resource 能力，消费订阅不推进 cursor。共享引用校验与能力检查保留在 Rust 内部；适配器的 raw::ProcessAccess 引用接口保持独立。

`process.wait()` 查询原生任务的完成状态；Python `handle.wait()` 消费并缓存该次订阅的完成结果。Pueue 的初始与额外订阅在建立时绑定可观测的原生执行，延迟消费也不跟随任务 ID 的重启或替换；wait/follow 前后发现执行改变返回 Conflict，任务清理返回 NotFound。正常的同一 Queued 执行开始仍可消费，已经交付的完成缓存继续属于原来的执行；SDK 不归档被替换的旧日志。关闭或丢弃订阅不终止任务，资源仍由调用方显式清理。

输出页保留原始 bytes 和 channel，Pueue 只有 Combined。默认 64 KiB，允许 1 字节至 1 MiB；读取立即返回当前观测范围，不等待后续输出或终结。`at_end` 只表示读到该次观测的尾部，进程是否终结单独 inspect。`OutputInfo.version` 是后端可提供的不透明输出代次标识，None 只表示没有可报告的标识，任务状态由 ProcessInfo 表达。cursor 绑定目标、任务、执行和文件版本，不含凭据或可指定的文件路径；只把它作为不透明字符串保存。在同一版本从同一位置可重读，新增字节是否进入这一页取决于该次观测的大小。

Pueue 尚未开始的任务返回空页、size=0、version=None，不读可能残留的上次日志；同一 Queued 执行的初始空 cursor 可以在其开始后继续使用，无法确认同次执行的状态转换返回 Conflict。原地重启、可检测的文件替换/截断返回 Conflict，原生任务/日志清理返回 NotFound，非法 cursor/limit 返回 InvalidArgument；SDK 不静默回到开头。文件身份和大小检查无法提供截断后在两次请求间重新写长的完整历史保证，外部修改原生日志后应重新查询。新接口不启动后台采集或持久化存储，也不安装/启停 pueued/envd。

## 生成与验证

协议来源与固定版本见 [`protocols/README.md`](crates/adapters/protocols/README.md)。上游文件由 `scripts/update-protocols.sh` 按固定版本下载并核对 SHA-256；Rust 通过 vendored protoc 在 Cargo `OUT_DIR` 生成。不手工改下载文件或生成代码。

流程测试需要真实进程、SSH、Pueue、tmux >= 3.5、kubectl、OpenSSL 和 Docker socket，必须在容器中运行，不以宿主机 skip 作为通过。构建测试镜像不会创建 Kubernetes 或云沙盒：

```sh
docker build -f Dockerfile.tests -t opensol-tests:latest .
docker run --rm \
  -v "$PWD:/workspace:ro" \
  -v opensol-target:/target \
  -v opensol-registry:/usr/local/cargo/registry \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e CARGO_TARGET_DIR=/target \
  -e OPENSOL_TEST_IMAGE=opensol-tests:latest \
  opensol-tests:latest
```

Docker socket 具有宿主机管理权限，只对可信代码运行。测试会创建并删除隔离的 Docker 容器、sshd、pueued 和 tmux server；不启动 Kapy。脚本执行格式检查、各 feature 编译、Rust 测试/Clippy/rustdoc、构建并安装 Python wheel，再运行 Python 绑定测试。Python envd fixture 使用同一固定 schema，由 grpcio-tools 在临时目录生成协议类型；它不是 wheel 的运行时依赖。

Local、SSH、Docker、Pueue、Tmux 使用真实运行时验证；Tmux 覆盖短命令退出、二进制输入、resize、渲染快照、代次绑定及注入目标边界。E2B/envd、sandboxd、OpenShell 使用固定 schema 的本地 HTTP/ConnectRPC/gRPC fixture；gRPC 另验证真实 TLS 的 CA/主机名校验和必需的 mTLS 客户端身份；agent-sandbox 使用真实 kubectl 对接本地 Kubernetes discovery/CR fixture。后者验证协议与参数边界，不代表已通过真实云服务、Kubernetes controller 或 OpenShell 部署的端到端认证测试。
