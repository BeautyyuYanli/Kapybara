# Execution 与 RPC 方案

本方案依据 `kapy_v2.md`、`docs/architecture.md`，以及 main 提交 `61af09e` 的 `docs/acceptance.md` 与 `.context/delivery.md`，面向 Linux、单个机器 daemon 和多个逻辑 session。Execution senior 负责 `src/kapy/execution/`、`src/kapy/rpc/` 及对应测试目录；Gateway 负责 CLI、配置读取、控制服务、远端机器 registry 与权限判定。本轮交付仅为方案，公共接口须经总设计师批准后实现。

## 1. 实现边界与依赖

使用已锁定的 Python 3.14.4、anyio 4.15.1、websockets 17.1、httpx2 2.12.0、platformdirs 4.11.7、pydantic 2.13.5。已运行 `uv sync --locked` 并检查安装包签名；不需增加 Python 依赖。PTY 使用标准库 `pty`、`os`、`termios`；SQLite 使用标准库 `sqlite3`，阻塞文件和数据库操作交由有界线程执行。asyncio 是运行后端，CLI 入口由 Gateway 选用 uvloop；不引入 free-threading。

`httpx2.AsyncClient.stream()` 支持 `content: AsyncIterable[bytes]`，响应支持 `aiter_raw(chunk_size=...)`，足够完成 presigned GET/PUT；本期不引入 OpenDAL。WebSocket 客户端使用实际存在的 `additional_headers`、`max_size`、`max_queue`、`write_limit` 参数。

模块保持四个主要职责：`rpc/peer.py` 管 duplex 连接，`rpc/messages.py` 共用 envelope/error/batch 处理；`execution/daemon.py` 组织本地服务和远端连接；`execution/processes.py` 管进程；`execution/files.py` 管传输。`execution/store.py` 管 SQLite/XDG，`execution/client.py` 提供 CLI 可调用的本地代理客户端，`execution/_launcher.py` 仅负责在用户代码执行前建立进程边界和 controlling terminal。类型放各包 `types.py`，`__init__.py` 只导出下述公共接口。

## 2. 公共 Python 接口

以下代码是拟议的导出契约，不是本轮新增实现。

```python
# kapy.rpc
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Protocol, Self

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type JsonParams = JsonObject | list[JsonValue]
type SendText = Callable[[str], Awaitable[None]]
type ReceiveText = Callable[[], Awaitable[str | None]]
type CloseTransport = Callable[[], Awaitable[None]]
type RequestHandler = Callable[[str, JsonParams], Awaitable[JsonValue]]

class RpcError(Exception):
    code: int
    message: str
    data: JsonValue
    def __init__(self, code: int, message: str, data: JsonValue = None) -> None: ...

class RpcDisconnected(Exception): ...
class RpcTimeout(Exception): ...

async def handle_request(
    body: bytes, *, handler: RequestHandler,
) -> bytes | None: ...

class RpcPeer:
    def __init__(
        self, *, send_text: SendText, receive_text: ReceiveText,
        close_transport: CloseTransport, handler: RequestHandler,
    ) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, tb: TracebackType | None,
    ) -> None: ...
    async def call(
        self, method: str, params: JsonParams, *, timeout: float = 60.0,
    ) -> JsonValue: ...
    async def notify(self, method: str, params: JsonParams) -> None: ...
    async def wait_closed(self) -> None: ...
    async def aclose(self) -> None: ...

class MachineCaller(Protocol):
    async def call(
        self, machine_id: str, method: str, params: JsonObject,
    ) -> JsonValue: ...
```

`MachineCaller` 是共享类型，具体 registry/caller 由 Gateway 实现，调用 deadline 由 Gateway 的实例配置控制，不增加公共 keyword 参数。Intelligence 持有该协议的实例，不直接导入 daemon。`params.session_id` 必须存在，Gateway 在路由前验证 session 与 machine 的关联。

```python
# kapy.execution
from pathlib import Path
from typing import Literal, TypedDict
import anyio
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from kapy.rpc import JsonObject, JsonValue

class SessionProxyAuth(TypedDict):
    kind: Literal["session"]
    session_id: str
    token: str

class UserProxyAuth(TypedDict):
    kind: Literal["user"]
    token: str

type ProxyAuth = SessionProxyAuth | UserProxyAuth

async def call_local_proxy(
    socket_path: Path, method: str, params: JsonObject, *,
    auth: ProxyAuth, timeout: float = 60.0,
) -> JsonValue: ...

class DaemonConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    machine_id: str
    gateway_url: str
    machine_token: SecretStr
    cgroup_root: Path | None = None
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

`gateway_url` 是完整机器 WebSocket URL；非回环地址必须使用 `wss://`。显式目录必须为绝对路径；idle 秒数为正数。`cgroup_root=None` 按第 5 节从专属 systemd user service 的实际委派位置发现；显式值只能固定为该发现结果，不能把任意 writable 路径当委派。Gateway settings 负责环境变量/配置文件映射，并构造 `DaemonConfig`；Execution 不自行加载 `.env`。`run_daemon` 拥有锁、数据库、socket、HTTP client、连接任务、进程/传输任务，在 stop、取消或退出时完成清理。构造配置不进行 I/O。

## 3. RpcPeer 协议与 transport 所有权

已连接的 transport 由调用方交给 peer，peer 在退出时调用 `close_transport()` 一次。`receive_text()` 返回一个完整 JSON 文本，返回 `None` 表示 EOF；transport 错误也导致断线。WebSocket adapter 将非文本消息视为协议错误；Gateway 用 Starlette `receive_text/send_text/close` 包装，daemon 用 websockets `recv/send/close` 包装。handler 的闭包绑定连接的认证身份，peer 不导入业务服务，也不从 params 推断权限。

进入 async context 时启动唯一 reader 和 writer；退出时清理这两个任务及当前连接的 handlers。接收循环立即处理 response，并发 dispatch request；不能等待某个业务 handler 完成才继续接收，否则 handler 内反向 `call()` 会死锁。写入统一串行。

`RpcPeer` 不另提供重复的 serve/run 启动方式。Gateway 在 `async with RpcPeer(...) as peer` 内注册已认证连接，然后 `await peer.wait_closed()` 持续服务；context 退出时注销 registry 并关闭 peer，显式 `aclose()` 可提前结束。`__aenter__` 完成后 reader/writer 已就绪，才允许 `call/notify`；重复进入或 context 外调用失败，不暗中重启已关闭连接。

HTTP `POST /rpc` 由 Gateway 限长读取原始 body、处理 HTTP 认证，然后调用 `kapy.rpc.handle_request(body, handler=...)`。返回 bytes 即 UTF-8 JSON-RPC 单个或 batch 响应，Gateway 用 HTTP 200/application/json 发出；返回 None 表示有效 notification 或全 notification batch，HTTP 204 无 body。无效 JSON/UTF-8、envelope、参数和业务 RpcError 均由 rpc 包编码；HTTP 认证失败和 body 超限分别由 Gateway 返回 401/403、413。该函数只接受 request/notification，传入 response envelope 作为 Invalid Request。它与 peer 共用解析、request 校验、dispatch、error 和 batch 编码，不通过虚拟 duplex transport 模拟 HTTP。handler 仍用闭包携带认证主体；HTTP 跨请求的总并发上限由 Gateway 控制，单次处理沿用下述 rpc 限额。

JSON-RPC 2.0 支持 request、response、notification、batch、命名及位置参数；缺省 params 归一为 `{}`。业务 machine/local/control 方法只接受命名参数。发送请求用连接内唯一字符串 ID；接收端保留请求 ID 的类型和值，区分缺失 ID 与 `null`。notification 无论成功或失败都不返回响应。禁止 NaN/Infinity，拒绝畸形 response，标准错误使用 -32700/-32600/-32601/-32602/-32603。[JSON-RPC 规范](https://www.jsonrpc.org/specification)

边界固定：单个 JSON message 1 MiB、JSON 嵌套不超过 64 层、batch 不超过 16 项、同时出站 call 64 个、业务 handler 64 个、待发送 message 64 个；每个响应及聚合 batch 编码后仍须满足 message 上限。过大结果返回资源限制错误，业务通过分页/chunk 拆分。达到 handler 上限时立即拒绝新 request，不能堵塞 reader；notification 丢弃并记录计数。writer 队列满时关闭连接，使两侧都得到失败，不无限排队。WebSocket 配置 `max_size=1_048_576, max_queue=4, write_limit=65_536, compression=None`。

`call` 超时移除 pending future，抛出 `RpcTimeout`；迟到响应忽略。断线令所有 pending call 立即抛出 `RpcDisconnected`，收束连接级 handler。peer 不跨连接保存 pending ID，也不自动重放请求。断线、超时、取消都不能证明远端副作用未发生。进程/传输由 daemon 拥有，handler 被取消不会杀死已启动的工作。业务状态通过以下稳定 ID 查询。

应用错误统一 `RpcError(code, message, {"kind": ..., ...})`：

| code | kind | 含义 |
| --- | --- | --- |
| -32001 | unauthorized | 凭证缺失、无效或无权限 |
| -32004 | not_found | 当前 session 不存在该资源，不泄漏别的 session |
| -32009 | conflict | ID 被用于不同参数、非法状态或 chunk offset 冲突 |
| -32010 | gone | 资源已显式释放 |
| -32020 | resource_limit | 并发、消息或磁盘资源不足 |
| -32021 | io_error | 文件/子进程/HTTP 失败，错误信息去除凭证 |
| -32022 | offline | 无可用机器连接或控制连接 |

## 4. XDG、持久化与 session 生命周期

默认目录由 platformdirs 计算：`$XDG_STATE_HOME/kapy` 存 `execution.sqlite3`、state lock 和 stdio spool，`$XDG_DATA_HOME/kapy/sessions/<sha256(session_id)>/cwd` 存工作目录，`$XDG_RUNTIME_DIR/kapy` 存 runtime lock 与 `daemon.sock`。XDG runtime 缺失时使用经过 UID/权限校验的临时 runtime 目录并告知调用方；不把大输出放进 runtime。新目录 0700，数据库、spool 与 socket 0600。[XDG 规范](https://specifications.freedesktop.org/basedir/latest/)

同一 state 根或 runtime 根只能运行一个 daemon，分别用文件锁阻止双实例；socket 只有持锁者可创建/清理。数据库绑定 machine_id，换机器身份时拒绝复用。session_id 为非空且 UTF-8 不超过 128 bytes 的不透明字符串，落盘名称由服务计算，不把外部 ID 拼进路径。process_id、transfer_id 为调用方生成的 UUID，所有查找同时带 session_id。

SQLite 开启 WAL、foreign_keys、busy_timeout；一个受锁保护的连接在有界线程执行短事务。用 `PRAGMA user_version` 管理包内 schema；不添加 ORM。表结构：

| 表 | 键与必要字段 |
| --- | --- |
| daemon_meta | 单行 machine_id、schema version |
| sessions | session_id PK、cwd、active/releasing/released、created_at |
| processes | (session_id, process_id) PK/FK、start 参数摘要、mode/cwd、state、cgroup 路径、exit_code、error、stdout/stderr 路径、PTY 已返回 tail/cursors、created_at/finished_at |
| transfers | (session_id, transfer_id) PK/FK、方向、参数摘要、path/staging_path、size、state、结果或错误、created_at/finished_at |

SQLite 存机器侧状态；控制面 session/input/output/history/event 的权威数据仍在 PostgreSQL。session token 仅驻留 daemon 内存；本地代理直接恒时比对，重启后 ensure 前不可使用该身份。presigned URL、HTTP headers、machine token、child env 不落入表或日志。

`session.ensure` 幂等创建 cwd 并把有效 session token 放入内存；每次重连由 Gateway 对仍关联的 session 重新调用，完成后才能启动该 session 的新命令。daemon 不自行创建控制面 session。不同 session 的 cwd、资源索引与环境独立；绝不在多任务 daemon 中调用 `os.chdir()`。start 的相对 cwd 基于 session cwd，省略时用默认 cwd；一个 shell 内 `cd` 只改变该 shell，不悄悄修改 session 默认 cwd。

进程 shell 本来能够访问此 Unix 用户的文件，因此这里是逻辑隔离，不是同 UID 下的安全沙箱。file API 的 path 是正常机器文件路径：相对路径基于 session cwd，绝对路径允许；不可借猜测另一个 session 的 process_id/transfer_id 访问服务保管的资源。

`process.release` 只对终止进程删除 spool/尾窗并保留 tombstone，防止旧 `process.start` 重试意外重跑。`session.release` 将 session 标为 releasing，阻止新工作，杀其所有进程、取消传输并清理该 session 的自有 cwd/spool、撤销内存凭证，最后标为 released。显式 push 到默认 cwd 之外的文件不随 session 删除。released ID 不重新用于创建。

## 5. 真实进程、PTY 与恢复

每个 managed process 有自己的 Linux cgroup v2，以覆盖 setsid/double-fork 后代。运行前提明确为：提供 cgroup v2 的 Linux、可用的 systemd user manager、支持 `DelegateSubgroup` 的 systemd 254+，以及 `cgroup.kill` 内核接口。普通用户通过专属 user service 获得委派，不需要修改 Lody unit 或手工 chown cgroup；仅看到挂载 rw、目录可写不能证明委派成立。systemd 的 `Delegate=yes` 建立管理边界，`DelegateSubgroup=daemon` 将管理进程放进单独叶组；Kapy 只管理 unit 根之下自己创建的 jobs 子树。[systemd delegation](https://github.com/systemd/systemd/blob/main/docs/CGROUP_DELEGATION.md)

在已导出 Gateway 约定的 `KAPY_CONTROL_URL`、`KAPY_MACHINE_ID`、`KAPY_MACHINE_TOKEN`，且 Kapy wheel 已构建的条件下，普通用户的启动形式为：

```sh
systemd-run --user --unit=kapy-execution --collect --wait --pipe \
  --service-type=exec \
  --property=Delegate=yes \
  --property=DelegateSubgroup=daemon \
  --property=KillMode=control-group \
  --property=SendSIGKILL=yes \
  --property=TimeoutStopSec=10s \
  --setenv=KAPY_CONTROL_URL \
  --setenv=KAPY_MACHINE_ID \
  --setenv=KAPY_MACHINE_TOKEN \
  "$(command -v uvx)" --from /absolute/path/to/kapy.whl kapy server
```

wheel 路径替换为实际产物路径；这里只按名字传入已有环境变量，token 不出现在 argv。该命令是批准后产品的启动契约，本轮不执行服务创建。`systemd-run` 创建独立的 transient user service；`--wait --pipe` 保持前台观察，`--collect` 在退出后回收 unit。Gateway 的 `kapy server` 从 control URL/machine ID 构造完整 WS URL 并调用 `run_daemon`。[systemd-run](https://raw.githubusercontent.com/systemd/systemd/v260/man/systemd-run.xml)

启动时通过 `/proc/self/cgroup` 和 cgroup v2 mount 信息定位当前实际 cgroup，只接受当前 `daemon` 叶组的直接父目录为候选 unit 根；要求该根具备 `user.delegate=1`、属于当前 UID，且显式 cgroup_root 若存在必须与它一致。不会向上遍历到 user@.service、user.slice 或猜测 UID/unit 路径。daemon 保持在 `daemon` 叶组，jobs 子树与它同级，既满足 no-internal-process 规则，也避免杀命令时杀到管理进程。不新增 CPU/memory controller 管理，也不修改 systemd 所有的 unit 根资源属性。

候选根确认后，在按 machine/state 根命名的专属 jobs 子树中创建唯一探测叶组，复用 launcher 的就绪握手，让无用户代码的探测进程迁入，验证可写 `cgroup.procs`、`cgroup.kill` 与 empty 状态，杀掉并回收该探测进程，清理叶组后才接收机器请求。验证的是自行创建的叶组，不要求写 systemd 所有的 unit 根 cgroup.kill。任何一步失败均停止启动、明确报告缺少的能力；不回退到 killpg，不先运行用户命令。专属验收 service 由总设计师创建，每次使用独立 unit/XDG 根；本 senior 不在 Lody 或其他 senior 的 cgroup 下试写。

正式启动命令时，先提交 starting 记录及专属 cgroup 路径并建立叶组，再启动小型 launcher：launcher 在执行任何用户代码前加入该组，经专用管道确认；daemon 持久化 running 转换后发出执行许可。许可管道 EOF 时 launcher 退出，防止半次 spawn 偷跑。恢复只清理当前已验证 unit 根内、本实例记录的 jobs 子树，不扫描或杀死任意系统 cgroup。

PTY 分配 master/slave，设置窗口大小；独立 launcher 调用 `os.login_tty(slave_fd)` 建立 session leader 和 controlling terminal，然后 exec argv。daemon 关闭自己的 slave，master 非阻塞读写，使用 anyio FD readiness。stdio 分离 stdout/stderr pipe，stdin 默认为 `/dev/null`，不增加不必要的终端模拟。shell 命令用显式 argv，例如 `["/bin/sh", "-lc", "..."]`。[Python os.login_tty](https://docs.python.org/3.14/library/os.html#os.login_tty)

避免在已有线程的 daemon 中使用 `preexec_fn` 或直接 fork 后运行复杂 Python 初始化；launcher 是独立解释器，只做建立边界和 exec。[Python subprocess](https://docs.python.org/3.14/library/subprocess.html#popen-constructor)

每个 PTY 持有最多 8192 **原始 bytes** 的环形尾窗，读出管道后持续更新，不能因为无人 wait 而停止 drain。累计 offset 按 bytes 计数；读取游标落在已淘汰数据前时返回最近可读位置和 `truncated=true`。不按 Unicode 字符截断；wire 使用 base64，展示端增量解码。已返回的尾窗/cursor 以及最终尾窗保存 SQLite，以便重启后解释已有游标。

stdio 两个 drain task 每次至多 64 KiB，追加到各自磁盘 spool，不使用 `communicate()`、整文件读取或无限内存队列。磁盘慢则有界背压到子进程。stdio 永不按 8192 bytes 截断，所有成功收集的 bytes 可按 cursor 读取，只有显式 release 才删除。空闲磁盘低于 64 MiB 或写入失败时停止该进程树并返回 `failed`、`output_complete=false`，保留已写部分，不能把不完整输出宣称为完整成功。

`wait_ms` 最大 30,000，0 是立即快照。PTY 在有新输出后连续安静 200 ms、进程结束、或截止时间到达时返回；没有新输出时等到退出或截止时间。stdio 等到退出或截止时间，不按安静推断完成。超时只结束本次观察。下一次 wait 带上前次 next cursor；立即读取也用 wait_ms=0。输入通过 `process.write` 写原始 bytes，`Aw==` 就是 Ctrl-C。由终端 line discipline 决定是否向前台进程组发送 SIGINT；raw mode 下只是字节，不承诺杀进程。

`process.kill` 写 `cgroup.kill=1`，覆盖变更 session/process group 以及 double-fork 的后代；这与 Ctrl-C 是不同操作。直到 cgroup empty、leader 已回收且输出 drain 完成才进入终态；超过 wait_ms 则返回 killing，可继续观察。正常 leader 退出但仍有后代时保持 running，保留已知 exit_code。内核负责 kill 与 fork 的竞争。[Linux cgroup.kill](https://docs.kernel.org/admin-guide/cgroup-v2.html#core-interface-files)

WebSocket 断线不影响进程。daemon 正常退出时清理全部下辖进程；专属 service 停止时 systemd 按 `KillMode=control-group` 兜底清理整个 unit，10 秒停止宽限后向仍存活进程发送 SIGKILL。daemon 异常退出后的下次启动只清理当前已验证专属子树中仍未终止的组，再把未正常完成记录标为 lost，保留 spool 和已提交 PTY 尾窗；属于旧 unit 的历史路径不能作为跨 unit 杀进程的依据。启动清理结束前不接收新请求。不依据可能复用的裸 PID 杀进程，也不伪装恢复已丢失的 PTY fd。主机重启后 cgroup 消失同样标 lost。此方案保证状态可恢复，不提供 daemon 重启后继续原终端的 attach 服务；信号尚未完成的任务不能提前宣称已清理。[systemd KillMode](https://raw.githubusercontent.com/systemd/systemd/v260/man/systemd.kill.xml)

默认最多 32 个活动进程、8 个传输；单进程 PTY 8 KiB、stdio 每路 64 KiB drain，加上固定 FD/协议缓冲，内存不随累计输出或文件长度增长。阻塞磁盘工作共享有界线程 limiter，不每条输出另建线程。

## 6. Machine RPC 完整表

以下均为 **Gateway → daemon**，通过 authenticated machine connection 路由；所有 params 都包含 `session_id: str`。没有另列的字段禁止传入。`?` 表示可省略，默认值写在表内，JSON 结果字段保持稳定。

通用结果与参数：

```text
Cursor = {pty: int}                         # PTY，默认 {pty: 0}
       | {stdout: int, stderr: int}         # stdio，默认二者 0
ByteChunk = {
  data_base64: str, start: int, next: int, available: int,
  truncated: bool, eof: bool
}
ProcessInfo = {
  session_id: str, process_id: str, mode: "stdio"|"pty", cwd: str,
  state: "starting"|"running"|"killing"|"exited"|"killed"|"failed"|"lost"|"released",
  exit_code: int|null, output_complete: bool,
  error: {kind: str, message: str}|null
}
ProcessUpdate = {
  process: ProcessInfo, reason: "quiet"|"timeout"|"exited"|"snapshot",
  output: {kind: "pty", pty: ByteChunk}
        | {kind: "stdio", stdout: ByteChunk, stderr: ByteChunk}
}
TransferInfo = {
  session_id: str, transfer_id: str, direction: "push"|"pull",
  state: "open"|"running"|"complete"|"failed"|"aborted",
  offset: int, size: int, sha256: str|null,
  error: {kind: str, message: str}|null
}
Transport = {kind: "websocket"}
          | {kind: "url", url: str, headers?: dict[str,str] = {}}
```

ByteChunk 的 start 是实际读取位置，next 是下一次 cursor，available 是采样时累计 bytes；eof 仅在流关闭且 next 到达末尾时为 true。stdio 的 truncated 始终为 false。负数 cursor、超过 available 的 cursor 是 -32602；进程未终止时即使暂时没有 bytes，eof 也不能为 true。exit_code 沿用 subprocess 的负信号编号，尚未获得时为 null。`output_complete` 只在完整收集且进程树终止后为 true，PTY 此值不否认正常的窗口淘汰。

| 方法 | 除 session_id 外的 params | result / 行为 |
| --- | --- | --- |
| session.ensure | session_token: str | `{session_id, cwd}`；创建/刷新；同 ID 不改变 cwd |
| session.release | wait_ms?: int = 5000 | `{session_id, released: bool}`；超时为 false，重复调用继续观察清理 |
| process.start | process_id: UUID, mode: "stdio"\|"pty", argv: list[str], cwd?: str, env?: dict[str,str] = {}, rows?: int = 24, cols?: int = 80, wait_ms?: int = 1000 | ProcessUpdate；重复同 ID/启动参数只观察，不重跑；wait_ms 不参与参数摘要 |
| process.wait | process_id: UUID, cursor?: Cursor, wait_ms?: int = 1000, max_bytes?: int = 65536 | ProcessUpdate；无破坏性观察，wait_ms=0 返回 snapshot；每路不超过 max_bytes，PTY 另受 8192 上限 |
| process.write | process_id: UUID, data_base64: str | `{accepted_bytes: int}`；仅 PTY，decoded 至多 64 KiB；只保证写入 fd，不保证应用已消费 |
| process.resize | process_id: UUID, rows: int, cols: int | ProcessInfo；仅 PTY，rows/cols 为 1..1000 |
| process.kill | process_id: UUID, wait_ms?: int = 5000 | ProcessInfo；幂等杀树，尚未完成则 killing |
| process.list | after?: UUID, limit?: int = 50 | `{items: list[ProcessInfo], next: str|null}`；按 process_id 排序分页，limit 1..100 |
| process.release | process_id: UUID | `{released: true}`；运行中 conflict，终止后删除输出，ID 留 tombstone |
| file.push | transfer_id: UUID, path: str, size: int, transport: Transport, sha256?: str | TransferInfo；将控制侧文件写到机器，size≥0 |
| file.pull | transfer_id: UUID, path: str, transport: Transport | TransferInfo；从机器普通文件读取，size 由 fstat 决定 |
| file.chunk | transfer_id: UUID, offset: int, data_base64?: str, max_bytes?: int = 65536 | push 为 `{next: int}`；pull 为 ByteChunk；仅 websocket transport |
| file.finish | transfer_id: UUID, wait_ms?: int = 1000 | TransferInfo；websocket 完成校验/提交，URL 等待完成或超时后返回当前状态 |
| file.abort | transfer_id: UUID | `{aborted: bool}`；关闭传输/删除临时文件，已完成返回 false |

start 的 argv 非空，禁止 NUL；PTY 只接受有效窗口尺寸，stdio 不使用 rows/cols。cursor 必须与 mode 匹配；max_bytes 为 1..65536。`start` 和 `wait` 每次 stdio 响应每路最多 64 KiB，PTY 最多 8192；输出更多时继续 wait_ms=0 读取。process.write 在断线后不能自动重发，因为输入不具备可重放语义；部分写入失败在 error.data 返回 accepted_bytes。

## 7. 流式文件传输

push/pull 的方向相对于控制面：push 是机器下载/接收，pull 是机器上传/发送。begin 使用稳定 transfer_id，重复相同参数返回当前 TransferInfo，参数不同返回 conflict；仅保存参数摘要，不保存 URL、headers 或原始 token。终态 begin 返回记录，不重启传输；URL 失败后需新 transfer_id 发起新的传输。执行中的 URL 传输是 daemon task，控制连接丢失不取消它。

WebSocket 每块 decoded 最大 65,536 bytes，base64 放 JSON-RPC 中；一次一个 chunk，得到响应才发下一块，使用原有连接，无第二条数据通道。push 的 data_base64 必须存在，不接受 max_bytes，不得写过声明 size；只接受当前 offset，重复最后一块时从暂存文件校验相同 bytes 后返回相同 next，不同内容 conflict。pull 无 data_base64，调用方持有 cursor，可读取 0..size 内任意 offset，使用 pread；ByteChunk.available=size、truncated=false，next=size 时 eof=true。pull 的 TransferInfo.offset 仅是已发送的最高位置，不能证明调用方收到所有字节。断线后 push 重复 begin 查询 offset，pull 用调用方已收到的 next 继续；不复制整文件。

push 写目标父目录内的唯一临时文件；不创建缺失父目录。所有 bytes 到齐、可选 SHA-256 匹配、fsync 完成后 `os.replace` 并 fsync 父目录，替换原目标。每个 destination 同时只允许一个 active push。打开与提交使用同一个父目录 fd，最终分量拒绝 symlink；已有硬链接通过 replace 断开，不原地覆盖 inode。中途失败删除临时文件，原目标保持完整。

pull 固定已打开的普通文件 fd，拒绝目录、FIFO、设备文件，记录 inode/size/mtime_ns/ctime_ns；传输期间和 finish 核对，变化则 conflict（reason=file_changed），不能宣称快照成功。读取本来被更改的文件不承诺快照隔离。pull 不计算全文件摘要，TransferInfo.sha256 为 null；调用方负责确认所需字节均已收到，再调用 finish 关闭句柄。push 仅在指定预期 SHA-256 时流式校验，并在结果中返回该摘要。文件 API 不对总大小施加 8192 限制。

URL push 使用 HTTP GET，URL pull 使用 HTTP PUT；headers 用于本次请求，禁止自带 Host/Transfer-Encoding。PUT 的 Content-Length 与已知 size 一致，冲突值拒绝；GET 不设置请求 body 长度。HTTP client 为 daemon 共享的 `httpx2.AsyncClient(trust_env=False, follow_redirects=False)`，TLS 验证开启，不携带 machine/session token，不跟随重定向。GET 设置 Accept-Encoding: identity 并通过 `aiter_raw(65536)` 写盘；拒绝非 identity 的 Content-Encoding，验证实际字节数。PUT 使用磁盘异步 iterator，response body 也流式丢弃，只保留至多 4 KiB 错误摘要。每次 I/O 无进展 60 秒失败，总传输不因文件大而强制短超时；HTTPS 必须，回环测试 URL 可 HTTP。

URL GET 没有读完整文件后才写盘的阶段，PUT 没有预读全文件的阶段。HTTP 失败、短读、size/checksum 不符、磁盘耗尽都留下明确失败结果；URL PUT 失败后远端对象是否已创建由存储提供方语义决定，不自动重试上传。一个传输停止活动 10 分钟后中止并删除 staging；daemon 重启将未完成 transfer 标 failed 并清理临时文件。本期不提供跨 daemon 重启的传输续传。

## 8. 本地 proxy、身份与 child context

本地 endpoint 是 `${runtime_dir}/daemon.sock`，AF_UNIX stream；每行一个 UTF-8 JSON-RPC 2.0 message，末尾一个 LF，JSON 字符串内换行必须转义。行长度含 LF 不超过 1 MiB，读入时就限长。多个请求靠 ID 关联，连接可以复用。仅相同 UID 的 peer 可连接；socket 权限和 SO_PEERCRED 检查作为本机边界。

Gateway CLI 调用 `kapy.execution.call_local_proxy(socket_path, method, params, auth=...)`，不自行实现 envelope、ID、error 或 framing。该函数拥有一次调用的 Unix connection/RpcPeer，返回业务 result 或抛出 RpcError/RpcTimeout/RpcDisconnected，finally 关闭连接；timeout 覆盖连接与等待的总时限，不自动重试。Gateway CLI 负责从环境/设置解析 socket_path 和 auth，并把 domain params 原样传入；客户端只包装 `proxy.call`，不读取 `.env`，不解析 CLI 命令。原始 token 不得进入客户端日志。

本地只公开 `proxy.call`，不把机器进程管理 API 暴露给任意本地 CLI。params 形状如下；session 身份与人工管理员身份二选一：

```json
{
  "jsonrpc": "2.0", "id": "cli-1", "method": "proxy.call",
  "params": {
    "auth": {"kind": "session", "session_id": "a7b01c24-f912-46f4-a3d0-c6772fe3b7e1", "token": "<session-token>"},
    "method": "session.input",
    "params": {
      "session_id": "d3e85be7-1d58-43cb-a1e5-612f137d37ab",
      "request_id": "3f943bbc-4777-441c-8e3b-9046c39b6c57",
      "payload": "...",
      "mode": "queue"
    }
  }
}
```

另一种 auth 为 `{"kind":"user","token":"<admin-bearer>"}`，由 Gateway CLI 显式提供；daemon 不保存管理员 token，也不允许省略 auth 自动成为管理员。示例内层方法参数由 Gateway/State 最终定义，外层 wire 固定为：

```text
ProxyAuth = {kind: "session", session_id: str, token: str}
          | {kind: "user", token: str}
ProxyParams = {auth: ProxyAuth, method: str, params: JsonObject}
```

控制面 `session.input` 参数采用 `{session_id, request_id, payload, mode?, waiting_id?}`：request_id 是调用方生成并在重试时保留的 UUID；payload 是 State 接受的 JsonValue，文本输入直接作为 JSON string，不另造 text 字段。JSON-RPC envelope.id 只关联本次连接内的请求/响应，不承担幂等职责。Execution 透传 request_id 与 payload；可信 caller scope 及 receipt 语义由 Gateway/State 处理，不在 proxy 中生成第二个幂等键。

daemon 对 session auth 与内存 token 作恒时比较，拒绝尚未 ensure 或 released 的 session，然后经远端 peer 调用 **daemon → Gateway** 的 `control.proxy(ProxyParams)`。`auth.session_id` 始终是调用来源；内层 `params.session_id` 若存在，是本次操作目标，两者不能混用或相互补全。Gateway 从已鉴权 WebSocket 获取 machine_id，验证 token 与 origin session/machine 绑定及内层目标权限；内层只接受 `session.*`、`event.*`、`history.*`、`skill.*`，不能再代理 proxy/machine/process 方法。Gateway 直接返回业务 JsonValue；本地返回相同 result，保留 CLI request ID。业务 RpcError 原样映射，连接错误为 offline；不把远端 request ID 或连接认证细节当作业务结果。

Gateway 下发 `session.ensure` 的完整 params 是 `{"session_id":"origin-session","session_token":"<session-machine-capability>"}`，result 是 `{"session_id":"origin-session","cwd":"<absolute-XDG-session-cwd>"}`。machine_id 来自调用所用机器连接，不在 params 中另传；cwd 由 daemon 创建，不由 token 或 CLI 指定。该方法仅存在于 Gateway → daemon 方向，和控制面的 session CRUD 方法分开 dispatch。

child env 固定携带：

| 变量 | 来源 |
| --- | --- |
| KAPY_MACHINE_ID | DaemonConfig.machine_id |
| KAPY_SESSION_ID | process 所属的 session_id |
| KAPY_SESSION_TOKEN | 最近 session.ensure 下发的该 session+machine 凭证 |
| KAPY_DAEMON_SOCKET | daemon.sock 的绝对路径 |

初始环境仅从 daemon 环境选取 PATH、HOME、LANG、LC_ALL、TZ、TERM 及 XDG_* 标准目录变量，再合并显式 `config.child_env` 和 start.env，最后注入保留 KAPY_* 变量；RPC env 禁止覆盖保留变量。不能复制整个 daemon 环境把机器 bearer/控制服务密钥带进命令。CLI 读取上述变量构造 session auth，不把 session token 写进 argv、结果或日志。Gateway 校验决定其能操作自身和授权后代，不能仅因同 machine 就读取无关 session。

session token 的签发、撤销和持久化由 Gateway 拥有；提议每个 session-machine 关联使用稳定 capability，关联删除/撤销立即失效。token 刷新只影响后续启动进程；既有进程保留原环境，旧 token 若被撤销，则其 proxy 调用返回 unauthorized。daemon 重启后先 ensure，再允许新的 start。

## 9. Outbound reconnect 与 idle

daemon 只主动连接 `gateway_url`，拟采用 Gateway 的 `/rpc/machines/{machine_id}` 路径，使用 `Authorization: Bearer <machine_token>` 与 `kapy.jsonrpc.v1` subprotocol；每个机器有独立 bearer，Gateway 验证凭证与路径中的 machine_id 匹配。`DaemonConfig.gateway_url` 传完整 URL，daemon 不自行追加路径。WebSocket ping/pong 20 秒，不作为 JSON-RPC 业务。一个 machine 只有一个 registry 中有效的 peer，重复连接 fencing/替换由 Gateway 完成。

网络断线按带 jitter 的指数退避重连，初始 1 秒，上限 30 秒，健康连接后复位；认证拒绝、错误配置或协议版本不匹配作为终止错误，不无限重试。重连创建新 RpcPeer，Gateway 重新 ensure session；命令和传输继续运行，stdio 继续落盘，PTY 继续维护尾窗。State/Intelligence 用原 process_id/transfer_id 查询，不重新执行副作用。process.start 的业务 ID、State 的 UUID request_id 负责各自领域的幂等。

默认 `idle_disconnect_after_s=None` 保持连接。启用后，仅在无活动进程、无传输、无 pending call/handler/local proxy，且达到业务空闲时间时断开。最多休眠 `idle_reconnect_after_s` 后主动重连；本地 proxy 到达可立即唤醒。没有独立唤醒通道，远端调用需要 Gateway 等待下次上线，受 Gateway 为 caller 配置的 deadline 限制；不能承诺离线瞬时可达。idle 关闭与远端请求竞争时按普通断线处理，不能隐式重放可能已执行的操作。

Gateway caller 对暂时 offline 的机器可在配置的 deadline 内等待一次可用连接；若超时返回 offline。daemon 接收本地 proxy 时唤醒连接并在 60 秒 call deadline 内等待；本地 caller 中断不撤销控制面可能已接收的写操作，UUID request_id 必须由 CLI/State 配合保持。

## 10. 跨模块要求

Gateway 接入 `RpcPeer` callbacks 和 `MachineCaller` 协议，实现 `control.proxy`、认证的 machine WS endpoint、connection fencing、按 session-machine 关联的 ensure/token 下发；HTTP `/rpc` 调用共享 `handle_request`，CLI 调用 Execution 导出的 `call_local_proxy`。CLI 命令参数、用户 API、Telegram 和管理员配置继续由 Gateway 拥有；Execution 不增加第二套 control dispatcher。

State 提供 session-machine 关联与删除状态，删除 session 前协调 machine `session.release`，离线机器保留待清理关联，重新连接后完成释放。控制面数据库中 session/input/history/event 不迁入 SQLite。递归 CLI 的 UUID request_id、waiting_id、权限范围和完成语义仍由 State/Gateway/Intelligence 协调。

Intelligence 调用 machine 表，选中 machine 后始终传 session_id；为 start/transfer 生成稳定 UUID，在输出中保留 process_id 与 cursors，明确 timeout 不是退出。stdio 大输出交给分块消费或 process_id 引用，不拼成无限长 tool response；PTY 的 truncated 必须展示。插件脚本走显式 argv，媒体走 file.pull 的 websocket 或 URL 路径。

总设计师提供符合第 5 节契约的专属 systemd user delegation 验收环境；Gateway 接入 `cgroup_root=None` 的自动发现和明确的启动错误，产品普通用户使用同一启动方式，不要求预先拥有任意 writable cgroup。共享 pyproject.toml、uv.lock、compose.yaml、README.md 不在本 senior 的修改范围。后续对应 tests/execution、tests/rpc 由本 senior 负责，并遵循总设计师验收清单中的 16 个并发交互任务、64 MiB stdio 输出和 64 MiB 双路径文件传输场景。文件完整性由发送端与接收端计算 hash 比较，不要求 pull RPC 新增完整 SHA-256 计算。需要跨模块 PostgreSQL/Valkey 时使用已 healthy 的共用服务，每次独立随机 schema/Valkey namespace 与独立临时 XDG 根；禁止重启共用服务、flush 共用 Valkey 或删除其他 scope 的数据。重启场景使用独立控制进程/schema 或专属可丢弃服务，不发送真实 Telegram 消息。产品验收与交付台账仍由总设计师维护，最终公共接口由总设计师统一批准。
