# Execution 剩余工作方案

以 main `8d42e77` 与指定增量 `fdf4cb642260d008f1d524a8d714f2c0482b9fd1` 的合并提交 `078ab79` 为基线，分支为 `feat/kapy-execution-complete`。保留已交付 RPC、XDG、SQLite/session 锁、FileManager 和 call_local_proxy。本方案取代旧 Execution 方案的 cgroup、宿主实验及 parent 环境继承假设；不合入 `9ec91c8` 或旧分支后续代码。

只修改 `src/kapy/execution`、必要的 `src/kapy/rpc` 及对应 tests/docs。复用现有依赖，不新增公共 RPC、不改共享配置；如实现确需依赖变更，由总设计师处理。

## 公共入口

从 `kapy.execution` 导出以下配置与入口，既有导出不变：

```python
class DaemonConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    machine_id: str
    gateway_url: str
    machine_token: SecretStr
    state_dir: Path | None = None
    data_dir: Path | None = None
    runtime_dir: Path | None = None
    child_env: dict[str, str] = Field(default_factory=dict, repr=False)
    idle_disconnect_after_s: float | None = None
    idle_reconnect_after_s: float = 30.0

async def run_daemon(
    config: DaemonConfig, *, stop: anyio.Event | None = None,
) -> None: ...
```

构造配置无 I/O；显式根目录必须绝对，idle 秒数必须有限且为正。Gateway 构造完整 `/rpc/machines/{machine_id}` ws/wss URL，daemon 不追加路径；允许 loopback ws，远程要求 wss。不存在 cgroup 字段、systemd/delegation 探测或相关 launcher。批准后先提交真实配置及导出，再告知 Gateway；本轮不改接口代码。

## ProcessManager

新增内部 `processes.py`，沿用冻结的 `process.start(mode)`、`wait/write/resize/kill/list/release` 参数、ProcessInfo/ProcessUpdate、Cursor 和 ByteChunk 形状。`wait_ms=0` 是即时读取；stdin 插件沿用 file transfer 加固定 shell 重定向，不扩展接口。

进程使用所属 session 的自有 XDG cwd，保留现有显式 cwd 语义，不新增进程工作目录层级；每个进程有独立进程组及有界 I/O 任务。最多同时运行 16 个任务，超限立即返回 resource_limit。stdio 分别把完整 stdout/stderr 流式写入私有 spool，分页读取每路至多 64 KiB；磁盘失败成为明确失败，不能伪称完整。PTY 具有真实 controlling terminal，支持输入、resize 和 Ctrl-C，内存只保留 8192 raw bytes 尾窗，返回真实 byte cursor 与 truncated。quiet 或等待超时只结束观察；进程继续运行，之后可继续输入和等待。

使用普通 process group；kill 终止该组并尽力清理可识别后代，回收直接子进程，不建立额外进程树跟踪或跨重启接管框架。PTY Ctrl-C 通过终端输入作用于前台任务。setsid/double-fork 逃逸不保证清尽；不为此引入 cgroup。主进程退出与输出流结束分别记录，残留持有管道的后代不能造成无期限资源占用；有限收束后输出不完整须显式反映，`output_complete` 不代表已证明所有逃逸后代消失。

start 以 `(session_id, process_id)` 和启动参数摘要幂等，先持久化 starting 再启动；同 ID 不重跑、参数冲突拒绝。只保存恢复所需状态、身份与输出元数据，不保存原始 token/env。扩展现有 SQLite schema/user_version，保存状态、exit/error、spool 路径与输出 cursor/PTY 已持久化尾窗。daemon 重启保留终态输出，把未终态记录转为 lost，不自动重新执行或声称接管旧 PTY；仅对身份仍能确认的旧进程做尽力清理，不能按裸 PID 杀可能复用的进程。process.release 仅接受终态，删除输出并保留 tombstone；中断清理可重试。

## Daemon 与 session 生命周期

新增 `daemon.py` 装配现有 store、FileManager、ProcessManager、HTTP client、Unix proxy 和 outbound WS。`run_daemon` 拥有全部资源；先持锁、恢复记录和未完成 release，再接受请求。stop/取消时停止接单，结束域任务和子进程、关闭 transfer/FD/client/socket，最后关闭 store 与锁。持久域任务归 manager/daemon，RPC handler 仅等待结果；连接取消不取消已接受工作，域任务数量有界。

session.ensure 创建自有 XDG cwd 并刷新内存 token。以 session 的短临界区协调 ensure、start/transfer 接受与 release，长时间执行/等待不持有该锁。release 先标 releasing、撤销身份并拒绝新工作，再结束进程及 transfer，清理自有 cwd/spool，最后标 released；失败保持 releasing 可重试，外部显式文件不删除。重启后须 ensure 才能接受新工作；token 刷新仅影响之后启动的子进程。

子进程环境只使用显式安全 `child_env`、冻结 start.env 中允许的显式值，以及最终注入的 `KAPY_MACHINE_ID/SESSION_ID/SESSION_TOKEN/DAEMON_SOCKET`。拒绝覆盖保留身份变量或传入控制面凭证变量；不复制 parent 环境或读取 .env，PATH/TERM 等需要时显式配置。caller session 与目标 session 始终分离。

复用 LocalTransport、validate_auth 和精确 ProxyAuth 类型，socket 为私有同 UID endpoint；本地只接受 `proxy.call`，session auth 校验内存 token，user auth 交 Gateway 验证，按原形转为 `control.proxy`，不自动重试未知副作用。连接与请求数量有界。

WS 使用独立 machine Bearer、`kapy.jsonrpc.v1` 和现有 1 MiB RPC 限额；关闭隐式环境代理。意外断线采用 1–30 秒带 jitter 退避，认证/配置/协议错误明确失败。每次连接新建 RpcPeer，已有进程和 transfer 留在 daemon，Gateway 重新 ensure。默认保持连接；启用 idle 后，只有无活动进程/transfer/业务请求/local proxy 且超过空闲阈值才断开，最多休眠 idle_reconnect_after_s，本地 proxy 可唤醒并在调用期限内等待连接。idle 与入站请求竞争按普通断线处理，不重放请求。

## 执行范围约束

实现阶段使用 cmd-impl，由本 senior 管理 persistent Elysia 与全部 Eden 五阶段，不将下级交给总设计师。真实机器、进程、PTY、文件与恢复场景只在 `kapy-v2-machine:dev` Docker 内执行，包括 16 并发、64 MiB stdio、既有双路径 64 MiB transfer、交互与断线生命周期。容器使用 `--rm --init --network none --memory 1g --pids-limit 128`，只读挂载 src/tests/pyproject，`PYTHONPATH=/workspace/src`、工作目录 `/workspace`，运行 `/app/.venv/bin/pytest`；URL server、XDG 与临时文件均在容器内。host 仅编辑、git、ruff/pyrefly 静态工作，不研究 Lody/宿主环境、不挂 socket/.env、不使用 privileged/hostPID。
