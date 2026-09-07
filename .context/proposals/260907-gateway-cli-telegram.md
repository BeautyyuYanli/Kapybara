# Gateway、CLI 与 Telegram 集成方案

本方案以 `kapy_v2.md` 和 `docs/architecture.md` 为边界，面向 Linux、单个控制进程、多 session、多 execution machine。实现范围为 `src/kapy/gateway/`、`src/kapy/cli/`、`src/kapy/settings.py` 及对应 tests。公共接口在总设计师批准后落地；本次提交只包含方案。

## 1. 依赖依据与模块边界

已执行 `uv sync --locked`，实际环境为 CPython 3.14.4、FastAPI 0.141.1、Starlette 1.6.0、httpx2 2.12.0、pydantic-settings 2.15.0、Typer 0.27.2、websockets 17.1、psycopg-pool 3.3.1、Valkey 6.1.1、Uvicorn 0.52.4。已通过本地 introspection 核对异步 HTTP、WebSocket、pool 和 settings alias API。现有依赖足够，不增加 Telegram SDK。

Gateway 只负责认证、路由、应用资源装配和前端适配。State 独占 session 串行化、输入缓冲、runner 调度、waiting、completion、output/history；Execution 独占 RPC 协议、daemon、进程、文件和本地代理；Intelligence 独占模型、runner、skill 内容及归档校验。

拟新增的职责文件：

| 路径 | 职责 |
| --- | --- |
| `kapy/settings.py` | 配置及显式环境变量别名 |
| `kapy/gateway/app.py` | FastAPI、lifespan、HTTP/机器 WS 入口 |
| `kapy/gateway/control.py` | 共用控制分发、参数校验、错误映射 |
| `kapy/gateway/machines.py` | 活跃 peer registry、双向调用、关联授权 |
| `kapy/gateway/auth.py` | 管理员、机器、session 调用者身份 |
| `kapy/gateway/telegram.py` | 前端插件、long polling、commands、输出发送 |
| `kapy/gateway/storage.py` | Telegram 表和参数化查询 |
| `kapy/cli/__init__.py`、`kapy/cli/commands.py` | `main`、Typer 命令、daemon/client 调用 |

## 2. Python 导出与应用生命周期

以下为拟议公共签名；签名中的 `...` 仅表示方案中的函数体省略。

```python
# kapy.gateway
from collections.abc import Callable, Sequence
from typing import Protocol
from fastapi import FastAPI
from kapy.rpc import JsonObject, JsonValue, MachineCaller
from psycopg_pool import AsyncConnectionPool
from kapy.settings import Settings

# MachineCaller 由 kapy.rpc 定义；Gateway 提供实现并可重导出。
# 本方案按总设计师要求使用 call(..., *, timeout=60.0)。

class ControlService:
    async def call(
        self, method: str, params: JsonObject, *, principal: Principal,
    ) -> JsonValue: ...

class Frontend(Protocol):
    async def run(self) -> None: ...

# frozen dataclass；仅用于 Gateway 自包前端的 metadata。
class FrontendContext:
    settings: Settings
    control: ControlService
    metadata_pool: AsyncConnectionPool

type FrontendFactory = Callable[[FrontendContext], Frontend]

def create_app(
    settings: Settings | None = None,
    *, frontends: Sequence[FrontendFactory] | None = None,
) -> FastAPI: ...

# kapy.gateway.storage
async def migrate(database_url: str, *, schema: str = "kapy_state") -> None: ...

# kapy.cli
def main() -> None: ...

# kapy.settings
def load_settings(*, env_file: str | None = None) -> Settings: ...
```

`Principal` 是 Gateway 定义、不可由 RPC 参数反序列化的 frozen dataclass：`kind: Literal["operator", "session", "telegram"]`、`machine_id: str | None`、`session_id: str | None`、`telegram_route: tuple[int, int] | None`。HTTP/WS 入口根据认证结果构造，Telegram 根据允许的 chat 和 durable binding 构造。`ControlService` 不接受远端提交的 `Principal`；机器连接身份只提供来源证明，不能单独变成 operator。

`create_app` 无 import-time I/O。lifespan 使用 `AsyncExitStack`：先分别执行 State 与 Gateway 的迁移，再创建 Gateway 自有 pool、SkillService、MachineRegistry 和 Intelligence runner；最后进入 `SessionService` 异步上下文并启动前端。State 根据 database_url/valkey_url 创建并拥有自己的 pool、Valkey、PubSub、锁连接和后台任务；Gateway 不借出自己的 pool 给 State，也不替它关闭资源。

Gateway 只创建并关闭自身 metadata pool 和 HTTP clients，不向 State 或 Skills 借出连接池。State/Skills 的资源由各自包创建并关闭，Gateway 只进入其资源上下文。lifespan 按顺序调用各模块迁移/初始化函数，每个函数只处理自包表；没有集中迁移框架、跨包 migration registry 或共享事务。Gateway 自有表使用 gateway_ 前缀。

registry 的授权 callback 通过 composition closure 引用已创建的 SessionService；所有对象装配完成前不接受请求。runner 使用 State 的 `SessionRunner = Callable[[RunContext], Awaitable[RunResult]]`，不另传 inputs/history；State 的 RunContext 已提供 inputs、state、checkpoint_number、poll_steer、emit、checkpoint、read_history。Gateway 不再定义替代 RunnerContext、checkpoint 或 RunResult。

一个 AnyIO task group 持有前端和 Gateway 清理任务，ASGI 持有机器连接任务。关闭时先停止前端与新请求，再退出 SessionService，使其取消并 await runner、关闭自身资源；随后关闭机器 peers、前端 HTTP client 和 Gateway pool。控制端停止不隐式杀死执行端 PTY。构造中途失败按相反顺序释放已获得资源。使用 FastAPI [lifespan](https://fastapi.tiangolo.com/advanced/events/)管理这些边界。

`frontends=None` 时按配置装配 Telegram；显式空序列不启动插件。CLI 和插件都通过 ControlService 调用同一业务层。FrontendContext.metadata_pool 仅访问 Gateway 前端 metadata，创建和关闭均由 Gateway app 负责。

## 3. 配置与身份

`Settings` 使用 `env_prefix="KAPY_"`，以下五项通过 `Field(validation_alias=...)` 保留既有名字；alias 不被前缀覆盖，已做离线构造核对，规则见 [Pydantic settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)。默认不自动查找或读入任何 `.env`；仅 `load_settings(env_file=...)` 显式载入指定文件。

| 环境变量 | Python 字段与类型 | 默认/含义 |
| --- | --- | --- |
| `OPENAI_BASE_URL` | `openai_base_url: str` | `https://api.openai.com/v1` |
| `OPENAI_API_KEY` | `openai_api_key: SecretStr \| None` | 空；启动模型服务时要求可用 |
| `OPENAI_MODEL` | `openai_model: str` | `gpt-5.6-luna` |
| `TELEGRAM_BOT_TOKEN` | `telegram_bot_token: SecretStr \| None` | 空；未配置时不启用 Telegram |
| `TELEGRAM_CHAT_ID` | `telegram_chat_id: int \| None` | 空；启用 Telegram 必须提供允许的 chat |
| `KAPY_DATABASE_URL` | `database_url: SecretStr` | `postgresql://kapy:kapy-local@127.0.0.1:55432/kapy` |
| `KAPY_VALKEY_URL` | `valkey_url: SecretStr` | `redis://127.0.0.1:56379/0` |
| `KAPY_DATABASE_SCHEMA` | `database_schema: str` | `kapy_state`；只能为合法标识符 |
| `KAPY_VALKEY_NAMESPACE` | `valkey_namespace: str` | `kapy_state` |
| `KAPY_CONTROL_URL` | `control_url: str` | `http://127.0.0.1:8000` |
| `KAPY_CONTROL_TOKEN` | `control_token: SecretStr \| None` | 管理员 bearer；控制服务要求配置 |
| `KAPY_MACHINE_TOKENS` | `machine_tokens: dict[str, SecretStr]` | 控制端机器 id 到独立 bearer 的 JSON 映射 |
| `KAPY_MACHINE_ID` | `machine_id: str \| None` | daemon 身份 |
| `KAPY_MACHINE_TOKEN` | `machine_token: SecretStr \| None` | daemon 连接控制端的凭据 |
| `KAPY_SESSION_SIGNING_KEY` | `session_signing_key: SecretStr \| None` | 控制端稳定 HMAC 密钥 |
| `KAPY_SESSION_ID` | `session_id: str \| None` | daemon 为 session 子进程注入的来源上下文 |
| `KAPY_SESSION_TOKEN` | `session_token: SecretStr \| None` | session 子进程调用代理的 capability |
| `KAPY_DAEMON_SOCKET` | `daemon_socket: Path \| None` | CLI socket override；默认由 Execution 的路径解析函数提供 |
| `KAPY_CGROUP_ROOT` | `cgroup_root: Path \| None` | 默认 None；Execution 从当前 unit 发现，显式值可指定当前 delegated scope/service 根并由 Execution 验证 |
| `KAPY_EXECUTION_STATE_DIR` | `execution_state_dir: Path \| None` | 映射 DaemonConfig.state_dir |
| `KAPY_EXECUTION_DATA_DIR` | `execution_data_dir: Path \| None` | 映射 DaemonConfig.data_dir |
| `KAPY_EXECUTION_RUNTIME_DIR` | `execution_runtime_dir: Path \| None` | 映射 DaemonConfig.runtime_dir |
| `KAPY_CHILD_ENV` | `child_env: dict[str, str]` | 默认空；显式允许的 child 环境，禁止日志回显 |
| `KAPY_IDLE_DISCONNECT_AFTER_S` | `idle_disconnect_after_s: float \| None` | 默认不主动 idle 断线 |
| `KAPY_IDLE_RECONNECT_AFTER_S` | `idle_reconnect_after_s: float` | 30.0 秒 |
| `KAPY_TELEGRAM_API_BASE` | `telegram_api_base: str` | `https://api.telegram.org`；允许注入本地 fake |

空的可选 secret 和 chat id 规范化为 `None`；必需项按 `server`/`control-server` 启动路径校验，CLI `--help` 不要求数据库或模型凭据。新增配置由总设计师加入共享 `.env.example`，本分支不修改共享配置。模型 context window 等 Intelligence 配置沿用其最终导出，Gateway 不猜测模型能力。

机器凭据通过控制端配置注册，无额外 enrollment 服务或用户账号体系。HTTP `/rpc` 校验管理员 bearer；机器 `/rpc/machines/{machine_id}` 在升级前校验该机器独立 bearer，并协商 `kapy.jsonrpc.v1` subprotocol。生产远程链路使用 TLS，loopback 开发地址保留 HTTP/WS。凭据不放 URL query、不回显 Settings、不记录 Telegram 带 token 的请求 URL。

session token 提议为 `v1.<session_id>.<machine_id>.<hmac>`，HMAC-SHA256 签名内容为固定用途前缀、canonical session id 和 machine id，签名校验使用 constant-time 比较。只有控制端持有 signing key；session 的存活与关联仍每次读 State。删除后 token 立即不能通过 session 存活检查；同 key 重启可重新导出同一 token，无明文 token 数据库和自动换 key。机器凭据本身不能伪造 session 身份。所有签名字段须使用无歧义编码，machine id 不允许包含 token 分隔符。

采用 Execution 的本地 NDJSON `proxy.call` → 机器 WS `control.proxy`，两端共用精确 envelope：

```text
ProxyAuth = {kind: "session", session_id: string, token: string}
          | {kind: "user", token: string}
ProxyParams = {auth: ProxyAuth, method: string, params: JsonObject}
```

`auth.session_id` 是来源，业务 `params.session_id` 是目标。session token 中的 machine id 必须等于已认证 WS 的 machine id。user token 是显式管理员 bearer，仅透传、不由 daemon 保存或补齐；缺失 auth 直接拒绝。只代理 session/event/history/skill 方法，禁止嵌套 control.proxy 或直接代理 process/file 方法。相同 OS 用户的进程不构成额外文件系统安全沙箱。

授权规则：operator 可管理该部署；session principal 必须关联承载连接的机器，只能操作自身和其直接创建的子 session，创建或更新可选机器都是来源 session 机器集的子集。递归由每个子 session 再创建下一层完成。Gateway 在自有 `gateway_session_access` 保存不可由用户改写的 owner_id 和 parent_session_id。Telegram principal 只操作其 route 的已绑定 session；`/new` 只能替换该 route 的绑定。新建 session 所选机器均须存在于控制端配置，非空 default_machine_id 必须在 machine_ids 中。

Gateway 的 mutation 先将 request_id、可信 principal、规范化参数及参数摘要持久预留到 gateway_requests，再调用 State；相同 UUID 来自其他主体直接拒绝。create 成功后在 Gateway 一笔事务中写入 access 事实和原始结果，再返回调用方。State 已提交而 Gateway 未提交时，启动恢复任务以同 UUID 和原参数重试获取首次结果，补齐 access；不要求跨模块共享事务。首次 session.ensure 等待该创建事实就绪，避免 runner 抢先派发时缺失来源授权；恢复任务在 State 可调用后补齐 pending create 元数据，MachineCaller 的就绪门同时等待这一步完成。lifespan 不等待离线机器上线，避免启动与机器 WS 建连互相等待。其他 State mutation 同样保存结果与目标归属；失败未产生 State 提交时可按同 UUID 重试。秘密认证 envelope 不进入 params 持久化字段。

owner_id 由 Gateway 可信 principal 生成；session 创建子 session 时继承 owner 但仅授予来源父 session 对该直接子 session 的操作权，owner 相同不自动授予其他 session token 全量权限。Telegram owner 包含 bot/chat/thread，receipt 授权查 gateway_requests，session 删除保留 access tombstone。授权元数据不属于 State DTO。

history、output、skill mutation 和 event 操作分别检查目标权限；知道 waiting id 不能获得任意 channel 的读写权。session token 可全局读取 skill 目录并创建新 skill，只能修改/删除自身创建的 skill；operator 可管理全部。该创建者元数据由 SkillService 持久化。

channel 的所有权和 grants 全部保存在 Gateway。`gateway_channels(waiting_id UUID PRIMARY KEY, creator_principal text)` 记录授权来源；`gateway_channel_grants(waiting_id UUID, principal_id text, can_publish bool, can_subscribe bool)` 以 `(waiting_id,principal_id)` 为主键。State 的 channel 仍只是 UUID 名称，不引入 ownership 表或 ACL DTO。

创建 session 时，Gateway 为默认 channel=session_id 建立创建者及该 session 的 grants。create/input 未给 waiting_id 时，Gateway 从 request_id 稳定派生 `uuid5(request_id,"completion-channel")`，先持久化 channel 与调用者 subscribe 权，再传 State；给定 channel 必须由调用者拥有或已有适当 grant。对已授权目标授予该次 completion 的 publish 权；同一 owner 可为多个合法目标使用同一 channel，支持多次完成及多个获授权订阅者。创建目标 id 由 State 返回时补齐对应 grant，首次 machine.ensure 前必须完成这一步。

Gateway 的可信授权入口为：

```python
async def authorize_channels(
    session_id: UUID, waiting_ids: Sequence[UUID], *,
    action: Literal["publish", "subscribe"],
) -> None: ...

async def grant_channel(
    waiting_id: UUID, target_session_id: UUID, *,
    can_publish: bool, can_subscribe: bool, principal: Principal,
) -> None: ...
```

grant_channel 只允许 channel 创建者向自身或有权控制的直接子 session 发放权限；operator 可管理该部署的 grants。该入口属于 Gateway 自有 metadata，不向 State 新增 grant 方法。Intelligence 的 wait 工具接收 authorize_channels 回调，在返回 wait_for 前验证 subscribe 权；Gateway 注入的 SessionRunner wrapper 在把 RunResult 交 State 前再检查，协议仍为 SessionRunner(ctx)。event.publish 校验 publish 权，producer_session_id 从可信身份注入；知道 UUID 不足以读写或订阅他人 channel。

## 4. 机器 registry 与双向代理

机器连接使用 Execution 的 `RpcPeer`，Gateway 不实现第二套 JSON-RPC codec。Starlette WebSocket 的 send/receive/close 回调交给 peer，handler closure 固定连接的 machine id。机器反向请求只接受 `control.proxy`，剥离认证 envelope 后执行控制方法。控制端发往机器的方法只允许 `process.*`、`file.*`、`session.ensure`、`session.release`；每个请求都包含目标 `session_id`。

新机器连接的处理顺序固定为：认证及 subprotocol 检查；进入 `async with RpcPeer(...)`，使 reader/writer 就绪；注册 initializing connection；从 State.list_sessions 的当前 session-machine 关联与 Gateway access/outbox 取得活跃关联；对这些关联通过原始 peer 主动调用 session.ensure；成功后才开放该 `(connection,session_id)` 的 readiness gate。ensure 是初始化调用，不通过尚未就绪的 MachineCaller 递归调用自身。

`MachineCaller.call` 对每次调用检查 session 存活、关联和 cleanup outbox，再等待该连接的 association gate，之后才发送 process/file RPC。已经恢复的 runner、原进程 CLI proxy、首次业务调用均走同一门；不能等它们先访问失败后才补 token。新创建的关联在 access/grants 提交后加入同一 ensure 流程。每个 `(connection,session_id)` 只有一个 ensure future，失败明确结束对应等待者，旧连接的 ready 状态不迁到新连接。

连接尚未 ready 时 RpcPeer 仍能收发 ensure 的 response，不能暂停 reader。恢复任务只在 MachineCaller 边界等待；app 可完成 lifespan 并接收 outbound machine WS，不要求 State 在进入上下文前等待机器在线。每个新连接主动恢复已有 token/cwd 关联，daemon 本地 proxy 不依赖一次新的 process.start 才能重新可用。

拟议 `session.ensure` 参数为 `{session_id, session_token}`，结果为 `{session_id, cwd}`；session_token 绑定接收机器，Gateway 内部注入，不来自模型 tool 参数。daemon 为进程注入 `KAPY_MACHINE_ID`、`KAPY_SESSION_ID`、`KAPY_SESSION_TOKEN`、`KAPY_DAEMON_SOCKET`。同机不同 session 分属不同 XDG cwd，Gateway 不接收任意本地 cwd 替代此规则。

registry 保存 `machine_id -> connection instance`，同 id 新认证连接替换旧连接；旧连接的 finally 只有在当前值仍指向自身时才能删除 registry，旧连接也不能继续发起控制请求。关闭旧 peer 时让待决调用以明确断连错误结束。远程执行请求没有拿到 response 时属于结果未知，Gateway 不自动重放有副作用的执行请求。

`MachineCaller.call(..., timeout=60.0)` 使用同一单调时钟 deadline 覆盖等待上线、session.ensure 和业务调用；不在每一阶段重新获得 60 秒。默认 daemon 保持连接；启用 idle 断线时使用 Execution 的 `idle_reconnect_after_s=30.0`，本地 proxy 可立即唤醒连接。deadline 内无连接返回 offline；断线后结果未知，不重放副作用。重连先 ensure 仍关联的 sessions，再恢复调用。

复用 Execution 限额：message 1 MiB、深度 64、batch 16 项、出站 call/handler/writer queue 各 64；reader 不等待 handler，过载及时报错而非堵住 response。WebSocket `max_size=1_048_576, max_queue=4, write_limit=65_536, compression=None`。文件 chunk 最大 64 KiB，长 stdio 继续使用磁盘 spool/cursor。

## 5. 控制 JSON-RPC 合约

HTTP `POST /rpc` 使用 Execution 的 `handle_request(body: bytes, *, handler: RequestHandler) -> bytes | None` 处理 envelope、batch、notification 和错误；`None` 对应 HTTP 204。WS/local 使用 RpcPeer，同一 ControlService 只接收 object 业务 params。所有 transport 均遵守 [JSON-RPC 2.0](https://www.jsonrpc.org/specification)。

采用 State 的原始 DTO，UUID 序列化为字符串、datetime 为 UTC RFC3339、tuple 为 array。以下标识分开：

| 标识 | 类型与用途 |
| --- | --- |
| `request_id` | 可信调用层为一次逻辑操作生成并保存的 UUID；State 按它去重，receipt 原样返回，session.wait 使用同一 UUID |
| `waiting_id` | 可多次投递的 UUID channel，供 agent wait tool 使用；不能判断某次请求是否完成 |

请求 envelope.id 仅关联一次 RPC response。相同 request_id 与相同参数重放首次结果；参数不同返回 Conflict。Telegram 使用 `uuid5(NAMESPACE_URL, f"kapy:telegram:{bot_id}:{update_id}:{action}")` 生成 request_id；action 为固定步骤名（如 new.create、message.input、settings.update），不包含会变化的配置。inbox 持久保存该 UUID、action 和原始参数，重启和重复 update 得到同一 UUID。State 的 create 参数指纹不含 Gateway 派生的 initial_state，重试保留首次成功保存的 skill 快照。Gateway 在调用 State 前检查 UUID 的归属，禁止其他主体复用或观察已有请求。

| DTO | JSON shape |
| --- | --- |
| `SessionView` | `{id,title,machine_ids,default_machine_id,config,status,run_id,cursor,created_at,updated_at}`；status 为 waiting/running/deleting；可空项同 State DTO |
| `Submission` | `{request_id,session_id,input_id,waiting_id}`；input_id 可空 |
| `CreatedSession` | `{session: SessionView, submission: Submission}` |
| `Completion` | `{run_id,outcome,output,cursor,completed_at}`；run_id 可空，outcome 为 completed/failed/deleted |
| `SubmissionStatus` | `{submission: Submission, completion: Completion\|null}` |
| `Record` | `{cursor,run_id,attempt,message_id,kind,data,text,created_at}`；run_id/attempt/message_id 可空 |
| `RecordPage` | `{items: Record[], next_cursor, has_more}` |
| `HistoryExportPage` | `{items: Record[],next_cursor,snapshot_cursor,has_more}` |
| `SessionPage` | `{items: SessionView[],next_after: UUID\|null}` |
| `EventReceipt` | `{request_id,event_id,waiting_id,delivered,pending}` |
| `QueryResult` | `{columns: string[],rows: JsonValue[][],truncated: bool}` |

### Session、输入、输出与等待

| 方法 | params | result |
| --- | --- | --- |
| `session.create` | `{request_id,title,machine_ids,default_machine_id,config,input?,mode?,waiting_id?}` | `CreatedSession` |
| `session.get` | `{session_id}` | `SessionView` |
| `session.list` | `{after?: UUID,limit?: int}` | `SessionPage` |
| `session.update` | `{session_id,request_id,title,machine_ids,default_machine_id,config}` | `SessionView` |
| `session.delete` | `{session_id,request_id}` | `{deleted: bool}` |
| `session.input` | `{session_id,request_id,payload: JsonValue,mode?,waiting_id?}` | `Submission` |
| `session.output` | `{session_id,after?: Cursor,limit?: int,wait_seconds?: float}` | `RecordPage` |
| `session.wait` | `{session_id,request_id: UUID,wait_seconds?: float}` | `SubmissionStatus` |

创建前由 Intelligence 的初始化入口提供 `RunnerState`，Gateway 组合 State `SessionSpec(title,machine_ids,default_machine_id,config,initial_state)`；可信 owner_id、parent_session_id 保存在 Gateway 自有授权表，不添加到 State DTO 或外部参数。默认机器可为 null；非空时必须属于 machine_ids。config 保存模型及非秘密配置，不保存 key/token。input=null 表示空创建，当场完成 receipt；有 input 的 create/input receipt 只在对应输入实际被该 run 接手后完成，不因 session 之前处于 waiting 而提前完成。

State submit_input 的服务默认是 steer；CLI/Telegram 普通输入显式传 queue，用户选择 steer 时原样传入。输入 payload 保持 JsonValue，不在 Gateway 改成另一种 envelope。普通文本使用字符串；其他格式由 runner 输入协议定义。

update 传完整可修改字段，只允许 State 的 waiting 且无活动 run；running 时返回 Conflict。Gateway 不承诺在运行间隙自动修改配置。session.list 的授权在 Gateway 完成：从自有 access 表取得自身/直接子 session 或 Telegram route 对应的授权 id 集，再传 State.list_sessions(session_ids=...)；operator 才能使用 None 查询全量。对外只接受 after/limit，不接受调用者伪造授权 id 集；沿用 State 返回的 next_after。

前端实时等待只走 `session.output` 的非消费 long-poll，after 从头重放到 next_cursor，再用 wait_seconds>0 接实时。`session.wait(session_id,request_id)` 由 Gateway 实现：先从自有 requests 校验归属并取得原始 Submission，再调用 State.read_output(after=...,wait_seconds=...)，顺序查找 kind=waiting 且 data.request_ids 包含该 UUID 的既有 Record；从其 run_id/outcome/output/cursor/created_at 装配 State 的 Completion/SubmissionStatus，不定义另一种结果 DTO。

该等待只有只读 output 操作，不注册 subscriber、不消费 events、不要求 State 新增 broker、completion 表或 wait_submission 服务。可从 requests 预存的提交前 cursor 开始，未保存时从头扫描；读完已有页后才使用剩余 deadline long-poll，超时返回 completion=null。State 当前已有 wait_submission 签名仅保留在 owner 合约引用中，Gateway 不把它作为依赖。

空创建的 input=null 从原始 CreatedSession 的 waiting 快照当场确定完成，无需等一次未来 run；普通成功/失败使用既有 waiting record 的 request_ids/outcome，不能只看到 session.status=waiting 就判某个排队输入已完成。删除导致 records 不可读时返回 Execution 既定 gone 错误；Gateway 仍保留请求授权和删除事实，不伪造已删记录或复制出新的 completion 存储。

### 事件与历史

| 方法 | params | result |
| --- | --- | --- |
| `event.publish` | `{waiting_id,request_id,payload: JsonValue,mode?}` | `EventReceipt`；producer_session_id 从认证身份注入 |
| `history.read` | `{session_id,after?,limit?}` | `RecordPage` |
| `history.search` | `{session_id,query,mode?: "substring"\|"fulltext",after?,limit?}` | `RecordPage` |
| `history.query` | `{session_id,sql,params?: JsonObject,limit?}` | `QueryResult` |
| `history.export` | `{session_id,after?,snapshot?,limit?}` | `HistoryExportPage` |

channel 使用 UUID 名称，无需先创建 channel 表；权限仍在 Gateway/Intelligence 的可信边界检查，UUID 不代替权限。普通 session.input 直接投给目标，event.publish 才做 MPMC 路由；不公开 event.subscribe/ack。completion data 使用 State `{type:"session.waiting",session_id,run_id,request_ids,outcome,output,cursor}`，waiting_id 可承载多次完成，request_ids 才关联具体请求。

history.read/search/export 不作为实时衔接入口。export 首页取得 snapshot_cursor，后续携带 snapshot 与 next_cursor，最后一页 cursor 到 snapshot；CLI 将有限页面写成 NDJSON。SQL 的命名参数使用 params object 原样交 State，Gateway 不拼 WHERE、不执行原 SQL，也不另写 SQL isolation。

State page limit≤200、wait_seconds≤30；单条输入/事件/model message≤256 KiB、delta≤16 KiB，页面及 QueryResult 上限512 KiB，给 Execution 的1 MiB message 留出 envelope 空间。Gateway 保留 State 的 next_cursor/has_more/snapshot，不再次切分或合并页面。单个结果或聚合 batch 编码后仍需满足 RpcPeer 上限，超限明确报 resource_limit。

### Skills

| 方法 | params | result |
| --- | --- | --- |
| `skill.list` | `{query?: string, cursor?: string, limit?: int}` | `{items: [{skill_id, description}], next_cursor: string\|null}`；query 子串过滤 |
| `skill.get` | `{skill_id}` | `SkillInfo` |
| `skill.read` | `{skill_id}` | `{skill_id, markdown: string}`；完整 SKILL.md |
| `skill.create` | `{session_id, machine_id?: string, archive_path: string, request_id}` | `SkillInfo` |
| `skill.update` | `{skill_id, session_id, machine_id?: string, archive_path: string, request_id}` | `SkillInfo` |
| `skill.download` | `{skill_id, session_id, machine_id?: string, archive_path: string, request_id}` | `{skill_id, archive_path: string, sha256: string, archive_bytes: int}` |
| `skill.delete` | `{skill_id, request_id}` | `{skill_id, deleted: bool}` |

skill 归档在获授权 execution session 的工作目录中交换，未指定 machine 时使用该 session 默认机器。Gateway 通过 Execution 已有 file.pull/chunk/finish 从 archive_path 获取有界 zip，再调用 Skills 原子 create/update；download 取同一版本完整 archive 后通过 file.push/chunk/finish 写回 archive_path。路径、覆盖规则与文件传输 hash 校验由 Execution 执行；不新增 skill upload 会话协议。

archive 上限拟 16 MiB、文件 chunk 上限 64 KiB，完整归档只在最终调用 SkillService 的有界 bytes 参数中出现。每次 Gateway 进程最多并行两次归档交换，临时文件在成功、取消或失败后删除；中断重新传输，不要求重启续传。Skills 保存 metadata 与 bounded archive bytea 的同一版本，负责 extraction 限额、归档验证和最终发布幂等。完整 SKILL.md 上限拟 64 KiB，确保全文可由单个 RPC 返回。CLI 的安全 pack/extract 复用 Intelligence 导出。

错误复用 Execution 的 RpcError：unauthorized=-32001、not_found=-32004、conflict=-32009、gone=-32010、resource_limit=-32020、io_error=-32021、offline=-32022。Gateway 将 State InvalidArgument 映射 -32602、ServiceUnavailable 映射 -32030，UnsafeQuery/QueryLimitExceeded 提议映射 -32040/-32041，避免与 Execution 代码冲突。error.data 含 kind/retryable 和安全上下文，不带 SQL、DSN、token 或原始堆栈。副作用结果未知时不自动重放。

## 6. Python 适配边界

直接复用 State 方案 §6 的 SessionSpec、SessionView、CreatedSession、Submission、SubmissionStatus、Record/Page、HistoryExportPage、RunContext、CheckpointWrite、RunResult、SessionRunner。State 服务签名如下，类型和默认值不另起一套：

```python
class SessionService:
    def __init__(
        self, *, database_url: str, valkey_url: str, runner: SessionRunner,
        schema: str = "kapy_state", namespace: str = "kapy_state",
    ) -> None: ...
    async def __aenter__(self) -> SessionService: ...
    async def __aexit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, tb: TracebackType | None,
    ) -> None: ...

    async def create_session(
        self, spec: SessionSpec, *, request_id: UUID,
        input: JsonValue = None, mode: InputMode = "queue",
        waiting_id: UUID | None = None,
    ) -> CreatedSession: ...
    async def get_session(self, session_id: UUID) -> SessionView: ...
    async def list_sessions(
        self, *, session_ids: tuple[UUID, ...] | None = None,
        after: UUID | None = None, limit: int = 100,
    ) -> SessionPage: ...
    async def update_session(
        self, session_id: UUID, *, request_id: UUID, title: str,
        machine_ids: tuple[str, ...], default_machine_id: str | None,
        config: JsonObject,
    ) -> SessionView: ...
    async def delete_session(
        self, session_id: UUID, *, request_id: UUID,
    ) -> bool: ...
    async def submit_input(
        self, session_id: UUID, payload: JsonValue, *, request_id: UUID,
        mode: InputMode = "steer", waiting_id: UUID | None = None,
    ) -> Submission: ...
    async def read_output(
        self, session_id: UUID, *, after: Cursor | None = None,
        limit: int = 200, wait_seconds: float = 0,
    ) -> RecordPage: ...
    async def wait_submission(
        self, session_id: UUID, request_id: UUID, *, wait_seconds: float = 0,
    ) -> SubmissionStatus: ...
    async def publish_event(
        self, waiting_id: UUID, payload: JsonValue, *, request_id: UUID,
        producer_session_id: UUID | None, mode: InputMode = "steer",
    ) -> EventReceipt: ...
    async def read_history(
        self, session_id: UUID, *, after: Cursor | None = None,
        limit: int = 200,
    ) -> RecordPage: ...
    async def search_history(
        self, session_id: UUID, query: str, *,
        mode: Literal["substring", "fulltext"] = "fulltext",
        after: Cursor | None = None, limit: int = 100,
    ) -> RecordPage: ...
    async def query_history(
        self, session_id: UUID, sql: str, *,
        params: JsonObject | None = None, limit: int = 200,
    ) -> QueryResult: ...
    async def export_history(
        self, session_id: UUID, *, after: Cursor | None = None,
        snapshot: Cursor | None = None, limit: int = 200,
    ) -> HistoryExportPage: ...

async def migrate(database_url: str, *, schema: str = "kapy_state") -> None: ...
```

Gateway 在 SessionService 外围负责 auth、参数 validation 和 JSON 化。State 默认 submit_input=steer、read_output limit=200/wait_seconds=0，与前端选择 queue/long-poll 分别显式传参。Intelligence 的 runner 适配为 State SessionRunner(ctx)；创建初始化返回 RunnerState 并写入 SessionSpec.initial_state。技能 description 快照与 codec 的生产归 Intelligence，Gateway 不拼装 Pydantic AI 历史。

Execution 已明确导出 RpcPeer、ProxyAuth、handle_request 和 call_local_proxy，直接使用这些名字。CLI 的 Typer 命令、参数与结果处理归 Gateway，NDJSON helper 使用 Execution 已接受的 call_local_proxy：

```python
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self
from pathlib import Path
from kapy.rpc import (
    JsonObject, JsonValue, JsonParams, RequestHandler,
    SendText, ReceiveText, CloseTransport,
    RpcError, RpcDisconnected, RpcTimeout, MachineCaller,
)
from kapy.execution import DaemonConfig, ProxyAuth

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

async def handle_request(
    body: bytes, *, handler: RequestHandler,
) -> bytes | None: ...

async def call_local_proxy(
    socket_path: Path, method: str, params: JsonObject, *,
    auth: ProxyAuth, timeout: float = 60.0,
) -> JsonValue: ...

async def run_daemon(
    config: DaemonConfig, *, stop: anyio.Event | None = None,
) -> None: ...

# kapy.rpc 的共享协议，按总设计师的 timeout 要求统一。
class MachineCaller(Protocol):
    async def call(
        self, machine_id: str, method: str, params: JsonObject,
        *, timeout: float = 60.0,
    ) -> JsonValue: ...
```

DaemonConfig 是 frozen/extra-forbid Pydantic model：必填 machine_id:str、gateway_url:str、machine_token:SecretStr；可选 cgroup_root/state_dir/data_dir/runtime_dir:Path|None，child_env:dict[str,str]={}，idle_disconnect_after_s:float|None=None，idle_reconnect_after_s:float=30.0。Gateway 从 Settings 显式构造；gateway_url 是 `/rpc/machines/{machine_id}` 的完整 ws/wss URL。非 loopback 使用 wss。cgroup_root 默认 None，由 Execution 从当前 unit 发现；显式值可传当前 delegated scope/service 根，实际委派和迁移能力由 Execution 验证。Gateway 不把任意可写目录当委派，也不自行修改主机权限。

CLI 的 socket/cwd 路径必须调用 Execution 的路径解析导出，建议 `resolve_socket_path(config: DaemonConfig) -> Path`、`resolve_session_cwd(config: DaemonConfig, session_id: str) -> Path`；可用于 CLI 的轻量路径配置形状由总设计师统一，不能为只找 socket 强制要求机器密钥。Execution 包内既有 platformdirs/hash 规则是唯一实现来源。

Skills 的以下签名是 Gateway 的适配需求；资源入口由 Intelligence 自包创建/关闭 pool，Gateway 只管理 async context 的进入/退出。文件 chunk 不进入 Skills 持久化协议。SkillInfo 形状为 `{skill_id,name,description,sha256,archive_bytes,created_by_session_id}`，最后一项可空；本方案要求 SkillService 持久化创建者事实，Gateway 据此授权。

```python
# 该资源入口由 Intelligence 提供，内部 pool 不外借给 Gateway。
def open_skill_service(
    *, database_url: str, schema: str = "kapy_state",
) -> AsyncContextManager[SkillService]: ...

class SkillService:
    async def list(
        self, *, query: str | None = None, after: str | None = None,
        limit: int = 100,
    ) -> SkillPage: ...
    async def get(self, skill_id: str) -> SkillInfo: ...
    async def read(self, skill_id: str) -> str: ...
    async def create(
        self, archive: bytes, *, created_by_session_id: str | None,
        request_id: UUID,
    ) -> SkillInfo: ...
    async def update(
        self, skill_id: str, archive: bytes, *, request_id: UUID,
    ) -> SkillInfo: ...
    async def get_archive(self, skill_id: str) -> bytes: ...
    async def delete(self, skill_id: str, *, request_id: UUID) -> None: ...

def pack_skill(source_dir: Path, archive_path: Path) -> None: ...
def extract_skill(archive_path: Path, destination: Path) -> None: ...
```

## 7. CLI 命令形状与上下文

main 的 `bce427d` 已提供 `[project.scripts] kapy = "kapy.cli:main"`，Dockerfile CMD 已为 `["kapy", "control-server"]`。Gateway 实现该既定入口，共享配置继续由总设计师负责。Typer 只解析命令，异步工作在一个 asyncio/AnyIO 运行入口执行；`control-server` 使用 Uvicorn 的 uvloop 配置，避免嵌套 event loop。

| 命令 | 动作 |
| --- | --- |
| `kapy server` | 调用 Execution `run_daemon`，管理本机和 outbound WS |
| `kapy control-server` | 运行 Gateway app，固定单 worker |
| `kapy control session create/get/list/update/delete` | 对应 session CRUD |
| `kapy control session input` | 提交文本，`--mode steer\|queue`、可选 `--waiting-id` |
| `kapy control session output --follow` | 带 cursor 重放后 long poll，逐条 JSON line |
| `kapy control session wait` | 有限等待 receipt，可重复读取 |
| `kapy control event publish` | 向获授权的 waiting channel 投递 |
| `kapy control history read/search/query/export` | session 范围历史；SQL 命名参数，export 输出 NDJSON |
| `kapy control skill list/get/read/upload/download/delete` | 全量/筛选描述、全文、完整 archive/folder |

`--session` 指定目标，未指定时从 `KAPY_SESSION_ID` 获取；来源 session id/token 始终独立取环境上下文。本地 socket 由参数、`KAPY_DAEMON_SOCKET`、XDG 默认依次决定。无 session 上下文时要求显式 `KAPY_CONTROL_TOKEN` 构造 user auth 透传；有 session token 时始终按 session 身份代理，即使环境中还存在 control token。CLI 默认经本地 daemon，不另造直连控制端的旁路。

mutation 默认生成并保留 UUID request_id，`--request-id` 允许重放；输出 State 原始 CreatedSession/Submission，明确包含 request_id、session_id、waiting_id。`session wait --request-id` 接收返回的 UUID，不接 waiting_id 冒充某次请求；`output --follow` 只推进 State cursor。长 prompt 和 SQL 支持 stdin/文件，token 仅用环境/受保护配置，不设计 argv token 选项。stdout 只输出结果/JSON lines，stderr 输出错误；Ctrl-C 仅停止本次 follow/wait，不删除 session、不打断远程进程。错误返回非零退出码；无事件的有限等待属于正常超时结果。

upload 在目标 session 的本机 XDG cwd 内暂存 zip，调用 `skill.create/update`，结束后删除自己创建的暂存文件。download 指定同一 session 内未存在的暂存 archive_path，待 file.push 完成后在本地校验 hash、安全解包并 rename 到目标目录；拒绝覆盖非空目标。CLI 通过 Execution 的 XDG 路径导出定位该 cwd，不复制目录算法；若使用远程 machine，RPC 返回文件在该机器的路径，CLI 不假定它是本机路径。pack/extract 的安全规则重用 Skills 导出。

## 8. Telegram routing、设置与持久化

Telegram 作为 `Frontend` 使用 httpx2 异步 Bot API client；只启用一个 long poller，`getUpdates(timeout=25, limit=100, allowed_updates=["message"])`，HTTP read timeout 大于 long poll timeout。`TELEGRAM_CHAT_ID` 是唯一允许的 chat，未配置不能进入开放接收模式；忽略其他 chat、机器人消息，非文本输入回复明确的文本输入提示并消费 update。普通消息/commands 都带相同 chat/topic 授权。群迁移时记录明确错误并保留状态，由配置更新允许的 chat；不自动改变 allowlist。

route key 为 `(bot_id, chat_id, thread_id)`，没有 `message_thread_id` 时将 thread_id 规范化为 0；发送时 0 省略，非零原样传回。不同 topic 的 config、active session 和 cursor 独立。saved config 保存 `{title,machine_ids,default_machine_id,config}`，初始模型取 OPENAI_MODEL；initial_state 由每次创建时的 Intelligence 初始化产生。`bot_id` 使用配置 token 的公开数字前缀，不保存 token。

Gateway 拥有以下 PostgreSQL 表定义和参数化查询；由 app lifespan 分别调用各 owner 迁移，State 不迁移 Gateway 表。schema 参数由连接 search_path 或受控 SQL Identifier 设置，不能插入未校验文本。

| 表 | 核心字段与约束 |
| --- | --- |
| `gateway_telegram_poll` | `bot_id PK, next_update_id bigint` |
| `gateway_telegram_inbox` | `(bot_id, update_id) PK, chat_id bigint, thread_id bigint, payload jsonb, resolved_action jsonb nullable, handled bool` |
| `gateway_telegram_routes` | `(bot_id, chat_id, thread_id) PK, session_id UUID nullable, config jsonb` |
| `gateway_telegram_delivery` | `(bot_id, chat_id, thread_id, session_id) PK, cursor text nullable, item_offset int, projection jsonb, next_attempt_at timestamptz nullable, blocked_error text nullable` |
| `gateway_session_access` | `session_id UUID PK, owner_id text, parent_session_id UUID nullable, deleted bool`；不可变创建来源，删除后保留 |
| `gateway_requests` | `request_id UUID PK, principal_id text, method text, params_hash text, params jsonb, target_session_id UUID nullable, start_cursor text nullable, result jsonb nullable`；写入预留、授权和恢复事实 |
| `gateway_channels` | `waiting_id UUID PK, creator_principal text`；仅 Gateway 的授权来源 |
| `gateway_channel_grants` | `(waiting_id,principal_id) PK, can_publish bool, can_subscribe bool`；State 不读取该表 |
| `gateway_session_cleanup` | `session_id UUID PK, request_id UUID, pending_machine_ids jsonb, state`；机器集可为空，仍可恢复删除阶段 |

poller 先用一笔 PostgreSQL 事务插入整批 inbox（主键去重）并持久化下次 offset，再向 Telegram 请求更高 offset；因此提前确认的 update 已在本地耐久保存。另一个处理循环按 route 顺序处理 inbox，先保存 resolved_action（目标 session、完整配置、payload、action 和 UUID5 request_id），再调用 ControlService；回复成功或确定为永久回复错误后标记 handled。崩溃发生在 State 提交之后、inbox 标记之前时，重放获得原结果，不重复创建 session 或输入。无关/不支持的 update 也明确完成，避免反复卡住 offset；临时错误保持待处理、定时退避，不阻塞其他 route。

route 每次只串行处理一个命令；`/new` 先以固定 request_id 创建 session，再提交 route 指针，恢复时仍得到同一 session。无 active session 的第一条文本按该 route 保存配置创建并提交 input。没有保存 machine 时：恰好一台配置机器则取它，否则发送设置提示并保持无 session；不猜测执行机器。

| command | 行为 |
| --- | --- |
| `/new` | 从该 route 已保存 config 创建新 session，旧 session 仍可查询 |
| `/settings` | 展示该 route 的非秘密配置 |
| `/model <name>` | 保存模型配置；active session 可更新时同步 |
| `/machine <id>` | 保存默认机器；必须来自已配置机器，更新 route 的 machine 集和 active session |
| `/instructions <text>` | 保存额外指令；active session 可更新时同步 |
| `/steer <text>`、`/queue <text>` | 明确输入模式；普通文本默认 queue |
| `/status` | session 当前状态 |
| `/help` | command 列表和简短参数说明 |

配置命令先持久化 route 的完整 saved config；active session 当前 waiting 且无 run 时再用固定 request_id 调 update。若 running，回复配置已保存、供 /new 使用，不隐式承诺该 run 中途更新；用户输入仍能继续处理。inbox 固定本次决定和目标参数，重放不改用后来的配置。新 session 不复制旧 history，只继承 saved config。`setMyCommands` 注册这些 commands；不存在 active session 时也可先保存设置。

### 输出发送和恢复

Telegram 只读取 State RecordPage，不进入 subscriptions。record.kind 原样采用 input、text_delta、tool_call、tool_result、notice、model_request、model_response、final、waiting、error、interrupted。`Record.message_id/run_id/attempt` 作为投影标识，不要求 State 新增 streamed 字段。

输出按约一秒或长度上限合并后追加纯文本，waiting/error/interrupted 及时刷新。Gateway 持久化当前消息投影（message_id、run_id、attempt、已接受可见正文）及发送位置；读取 delta 拼成可见文本，model_response 的完整 text 替换同 message_id 的投影。完整正文与已发送前缀一致时只追加剩余文字，不重发整段；若内容不一致则明确追加更正。final.data.output 作为最终结果，仅在未由最后一条响应呈现时补发；waiting 展示状态。interrupted 明确标记未完成 attempt，后续 attempt 使用新的投影身份。

单消息投影受 State 256 KiB message 限制；每条发送正文≤4000 UTF-16 code units，保持 Unicode 字符完整并预留 session 标注空间。大型输出继续由 State records 分页承载，不一次读全历史。各 topic 共用 chat 限流，429 尊重 retry_after。Bot API 的 topic、文本上限与 offset 确认规则以[官方文档](https://core.telegram.org/bots/api)为依据。

cursor 只在所覆盖文本成功发送或明确为不可见记录后推进；item_offset 保存部分记录的已发送字符数。投影和 cursor 同事务保存，重启能继续比较完整 message 与已输出前缀。`/new` 不删除旧 session，旧 delivery 可继续发送并附 session 简短标识。网络/5xx 采用 1–30 秒 jitter 退避；next_attempt_at 持久化。401 停用 bot；403/topic 不可发仅阻塞对应 route，不热循环，后续该 route 有效输入可触发恢复。send 已成功但 response 丢失或本地 cursor 提交前崩溃可能重复，不能承诺 exactly-once。

### session 删除与机器清理

按 State 语义，delete 会取消 runner 并物理删除 session records；Gateway 不要求 State 保留被用户删除的历史。Gateway 删除前用自己的短事务保存 immutable session/machine 关联及授权事实到 `gateway_session_cleanup`，并暂停对应 Telegram delivery；随后调用 State.delete_session。State 返回 deleted 后终止该 session 未发送 delivery，Telegram 已发送内容保持原状，后续等待根据保留的删除事实返回 gone，不依赖已物理删除的 output。

清理 worker 使用持久任务内的关联调用 `session.release({session_id,wait_ms:5000})`；released=false 继续观察，离线机器保留任务，重连后继续。cleanup 只允许 release，不向已删除 session 发送 process/file/ensure。每台机器释放成功后标记完成，所有阶段按原 request_id UUID 可恢复；启动时扫描未完成任务，不依赖 State 已删除的 session 行。该表、worker 及权限归 Gateway，不把删除机器职责塞回 State。

## 9. 持久化边界与集成约束

PostgreSQL 是 session、输入、history/output、event、completion 和 Telegram ingress/delivery 的权威存储；Valkey 只提供唤醒提示，Gateway registry 是连接事实的内存映射。没有将控制状态改用 SQLite/内存的路径。Execution 的 XDG SQLite/进程资源归 Execution。

每次集成调用使用独立 `KAPY_DATABASE_SCHEMA=gw_<uuid>`、`KAPY_VALKEY_NAMESPACE=gw:<uuid>`，使用总设计师提供的 PostgreSQL/Valkey 地址；清理只作用于自己的 schema/namespace。Gateway 与其他 senior 不共享测试路由、session ids、上传临时目录或 XDG root。Telegram send/getUpdates/setMyCommands 全部对 fake Bot API 或 MockTransport，不发送真实消息；本轮没有读取主目录 `.env`。已只读查看 main 的 `docs/acceptance.md`（61af09e），按其 Gateway 并发 polling、机器中断、Telegram 恢复场景提供对应 module tests；不重启或 flush 共用开发服务。

跨模块交付要求：State 提供本方案直接采用的 DTO、UUID request_id/receipts、非消费 read_output/read_history、history.export 和独立迁移；Execution 提供 RpcPeer/handle_request/call_local_proxy、MachineCaller timeout、DaemonConfig/run_daemon、ensure/release 和路径解析；Intelligence 提供兼容 State SessionRunner 的 runner、channel 授权回调接入、创建时 initial_state 和自持资源的安全 Skills 服务。总设计师统一剩余接口、错误码和配置示例；CLI script 与 Docker CMD 已在 main 完成。Gateway 不并行修改其他 senior 包或共享 `pyproject.toml`、`uv.lock`、`compose.yaml`、README。
