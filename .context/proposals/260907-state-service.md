# State：持久 Session、事件与隔离历史查询

本方案以 `kapy_v2.md` 和 `docs/architecture.md` 为准，并对齐 main 提交 `61af09e` 的 `docs/acceptance.md` 与 `.context/delivery.md`，范围是 `src/kapy/state/`、`tests/state/` 及 State 文档。当前只提交设计；公共契约经总设计师批准后，才进入 cmd-impl。模型运行、工具、压缩和技能属于 Intelligence；认证、机器 registry、前端与应用装配属于 Gateway。

## 1. 已确认的技术基础

2026-09-07 在本 worktree 执行 `uv sync --locked` 成功：Python 3.14.4、psycopg 3.3.5、psycopg-pool 3.3.1、valkey 6.1.1、sqlglot 30.18.0、pydantic-ai-slim 2.40.0。代码仓库只有初始脚手架，尚无 State 实现。

只读探测确认指定开发 PostgreSQL 为 17.11，仅安装 plpgsql，开发角色为 superuser；指定 Valkey 的 PING 成功。没有读取 `.env`。后续 State 集成使用每次随机的 PostgreSQL schema 和同名 Valkey 前缀，不共享表、频道、迁移锁或清理范围。恢复场景只重启隔离的 control process，或使用专属可丢弃服务；禁止重启共享 PostgreSQL/Valkey，禁止 FLUSHDB/FLUSHALL 或删除其他 owner 的数据，清理只针对本次创建的 schema 和精确 namespace。

直接使用 psycopg 异步连接池、参数 SQL 和 `dict_row`，数据库读出只做 TypedDict/cast 或无校验 dataclass 装配，不经过 Pydantic/ORM validation。Gateway 校验外部参数，State 检查业务约束。pool 使用 `open=False` 并显式 `await open()`；连接和事务由 State 所有。[psycopg 连接 API](https://www.psycopg.org/psycopg3/docs/api/connections.html)

使用现有依赖，不新增 ORM、队列框架、数据库扩展或集群组件，不修改共享 pyproject、uv.lock、compose 或 README。

## 2. 数据模型与迁移

使用一个由装配层指定的 schema，默认 `kapy_state`。不按 session 建 schema/表。迁移位于 `kapy/state/migrations/`，是有序、人工编写的 SQL 源文件，不是生成文件。`schema_migrations(version, checksum)` 记录已执行版本；迁移在事务内获取该 schema 的 advisory lock，逐版本执行并核对 checksum。只创建本 schema 的对象；数据库和登录角色由总设计师提供，不在服务启动时 CREATE DATABASE/ROLE。

| 表 | 主键、主要内容与约束 |
| --- | --- |
| `service_meta` | 单行，`epoch UUID`；短写事务先锁此行并检查当前 epoch，形成统一提交顺序 |
| `sessions` | `id UUID`；不可变 owner_id、parent_session_id 可空；title、machine_ids JSONB、default_machine_id、config JSONB、initial_state JSONB、status、latest_run_id、next_seq BIGINT、created_at/updated_at；status 为 waiting/running/deleting |
| `runs` | `id UUID`，session_id FK；attempt、status running/waiting/failed/interrupted、checkpoint_no、runner_state JSONB、started_at/finished_at；每 session 至多一个未结束 run |
| `inputs` | `id UUID`，session_id FK；event_id 可空、mode steer/queue、payload JSONB、seq、run_id 可空、state pending/reserved/consumed；`UNIQUE(event_id, session_id)` 防止重复事件投递 |
| `records` | `(session_id, seq)`；run_id/attempt/message_id 可空、kind、data JSONB、text、created_at、search_vector；兼作持久输出流和历史，避免两份游标事实源 |
| `subscriptions` | `(channel_id, session_id)`；持久订阅。默认频道订阅不可移除；外部频道由 wait_for 替换 |
| `events` | `id UUID`；channel_id、producer_session_id 可空、mode、payload、state pending/delivered、created_at；pending 表示尚无合格接收者 |
| `requests` | `id UUID`；`UNIQUE(key_scope, request_key)`；operation create/update/delete/input/publish、参数指纹、返回 receipt JSONB、target_session_id 可空、input_id 可空、waiting_id 可空、completion JSONB 可空；用于重试去重及 completion 非消费观察 |

频道直接由 UUID 标识，不另建只有 id 的 channel 表；session 默认频道就是 session_id，其他 waiting_id 由调用方提供或由 State 分配。events/subscriptions 持久保存频道名，频道无需先创建。UUID 不充当顺序号。所有 session 记录和输入顺序由同一事务中的 `sessions.next_seq` 分配；事务回滚同时回滚计数，后来的已提交游标不会越过较早未提交记录。不同 session 的顺序互不关联。

索引包括 inputs(session_id, state, mode, seq)、events(channel_id, created_at, id) WHERE pending、subscriptions(session_id)、records(session_id, kind, seq)、records 的部分 GIN(search_vector)。输入、run、记录和订阅使用 session_id FK 并随 session 删除，其他 session 已接收的事件输入不级联删除。parent_session_id 是不可变的创建来源标识，不随父 session 删除清空；requests 的目标标识也不级联删除。requests 保留原操作返回值、必要的 owner/parent 来源与 completion，使重放不会复活已删除 session，且删除通知仍可观察。

## 3. 运行与资源所有权

Gateway 在应用 lifespan 中创建并进入 `SessionService`，退出时关闭它。State 拥有 pool、一个 Valkey 客户端及 PubSub、后台协调任务和每 session 至多一个 runner task；runner 回调由 Intelligence 注入。不同 session 的模型/机器 I/O 并行；同一 session 的 runner 永不重叠。状态写入使用短事务串行提交，不把模型、机器 RPC、等待 hint 或用户 long-poll 放进数据库事务。

总设计师的验收负载为 100 个 fake-runner session、每个 20 条输入，以及向 100 个 listener 广播。实现保留上述并发模型，按该负载记录 accepted/completed/replayed 数量、session 内顺序、耗时、吞吐和事件完成延迟；不把 Valkey hint 计作实际投递，也不凭空承诺吞吐门槛。这些是批准后需要提供的证据，当前尚未执行。

启动取得 schema 专属的 PostgreSQL session advisory lock，拒绝同 schema 的第二个 control process，更新 service epoch 后恢复持久任务。独立锁连接断开即停止接收变更并取消本地 runner；每个写事务再核对 epoch，每个 runner 写入还核对 run_id/attempt，拒绝旧进程或旧 attempt 的迟到结果。

每次提交后通过真正的 `valkey.asyncio.Valkey.publish` 发 `namespace:wake` hint，内容仅为受影响 session/channel id；hint 不承载事实或 cursor。PubSub 只用于缩短延迟。协调器启动、重连和每隔最多 1 秒都扫描 PostgreSQL 中可运行的 session；output long-poll 也每秒重查。Valkey 发布失败、订阅断线、hint 丢失均不回滚已成功的业务事务，也不会使工作永久滞留。Valkey Pub/Sub 本身不保证离线补发，可靠性因此放在 PostgreSQL。[Valkey Pub/Sub 语义](https://valkey.io/topics/pubsub/)

关闭顺序为停止新请求、取消并 await runner、取消协调/long-poll、unsubscribe 并 aclose PubSub/client、关闭 pool 和锁连接。仅取消内存监听不会删除持久 subscriptions。未正常结束的 run 保留恢复所需状态，不伪造 waiting completion。

删除先在事务内记录请求 key/指纹并标记 deleting，使 runner 写入失效，然后事务外取消并 await runner，再事务内完成未完成请求的 deleted completion、移除订阅和 session 数据，并保存 delete 返回值。删除未结束时请求结果为空；同键重试加入或恢复这次删除，不另开一次删除。启动扫描会继续未完成的 deleting。删除不负责杀 execution 进程、删 machine cwd 或删 skill；Gateway/Execution 按各自所有权处理。

## 4. 输入、检查点与 waiting

`session.input` 是明确投给目标 session 的持久输入；其 waiting_id 是本次请求完成通知的频道。`event.publish` 则走 MPMC 广播路由；向 session_id 频道 publish 即可使用默认输入 channel。前端常规输入走 session.input，避免把用户 prompt 广播给监听 completion 的 session。

waiting session 有 pending 输入时创建 run，按 seq 取至多 64 条 steer/queue 并标记 reserved。running 时 `poll_steer` 仅保留当前新到的 steer；queue 等下次 waiting。reserved 不是已消费：只有与 runner checkpoint 同事务提交后才标记 consumed。最后一次 poll 之后到达的 steer 不会丢失，保留给下一轮。

runner 在模型/工具边界提交 checkpoint：原子更新 runner_state、追加完整 model message 历史、确认本次消费的 input ids。State 不理解压缩结构；Intelligence 拥有带 codec 版本的 JSON 状态，包含当前可恢复模型上下文、压缩级别和未决工具调用。原始历史只追加，压缩不会修改或删除原始记录。

正常返回的最终事务执行：检查 attempt 和 checkpoint 序号；提交最终 checkpoint 和 final output；将 run/session 置为 waiting；替换外部 wait_for、保留默认订阅；处理新订阅频道 backlog；为本 run 已消费的 create/input 请求分别发 completion；向 session 默认频道发一次 waiting completion。全部一起提交。queue 或 backlog 即使已可运行，也必须经过这个真实 waiting 边界再开启下一轮。

completion 只对应实际进入该 run 且已确认消费的输入。运行中刚到、尚未交给 runner 的 queue/steer 不会被提前完成。同一 request 的 completion 只生成一次；同一 waiting_id 可关联多个请求并多次发布。一个 run 合并多个请求时，各请求按各自 request_id 收到完成信号。若请求 waiting_id 恰为目标 session 默认频道，合并为该次默认 completion，payload 中保留所有 request_ids，避免同一频道双发。

create/input 的 request receipt 在同一 waiting 事务中写入 completion JSONB；`wait_submission` 只读取这份持久结果，不注册 subscriptions、不改变 events 状态、不弹出 inputs。多个 UI/CLI 可反复观察同一 receipt，agent 是否已消费频道事件不影响观察结果。超时返回 completion=null，未知请求或 target 不匹配报 NotFound；只有已完成、失败或删除才返回对应终态。按 waiting_id 本身不能确定某次请求是否完成，观察始终使用 session_id+request_id。

空 session 创建即处于 waiting，create receipt 当场完成；携带初始 input 的创建在首次消费该 input 的 run 进入 waiting 时完成。自然模型结束等价于 wait_for=()，始终保留默认输入订阅。runner 最终返回时不得遗留自己 reserved 但未处理的输入；State 将其视为 runner contract error。

runner 普通异常结束当前 run：持久 error、置 session waiting，并用 outcome=failed 完成该 run 已接手请求，避免自动无限重试。取消或进程中断不同于模型失败：恢复时仍使用原 run_id，attempt 加一，从最新提交 checkpoint 继续，把未确认 reserved 输入重新交给 runner，已消费输入仅存在于恢复上下文中。

State 不宣称 PostgreSQL 事务能使外部命令 exactly-once。Intelligence 必须在派发工具前 checkpoint 工具意图及 call id，拿到结果后 checkpoint 结果；重启时若结果未知，优先用 Execution 的已有 process/request id 查询。无法确定的调用以可识别的 outcome_unknown tool result 修复模型协议后继续，不盲目重发命令。未完成 attempt 的 delta 留在输出记录中，并追加 interrupted 记录，使前端能标记部分输出。

## 5. Sticky MPMC 事件

publish 与订阅修改在同一 schema 的短写事务锁下线性化。若存在除 producer_session_id 自身之外的订阅 session，则为当时所有合格接收者各插入一条 inputs，按事件指定 steer/queue，随后把事件置 delivered；这次持久交接就算消费，无需另设 broker ACK 表。唯一约束使事务重试不会多投。

没有合格接收者时事件保留 pending，包括只有 producer 自己订阅的情况。新增订阅时，按频道事件顺序把 pending 事件投给当时全部合格接收者，再置 delivered。随后才订阅的 session 不追溯已经 delivered 的事件；若第一次订阅后又加入另一 session，也不把已交接事件重新广播。旧 backlog 存在时，新 publish 先尝试排空同频道旧事件，不能越过可投递的旧事件。

wait_for 是持续订阅集合，唤醒不会消费订阅本身；runner 工作期间仍可多次接收事件，按 mode 进入 steer 或 queue。只有下一次成功返回的新 wait_for 或 session 删除会移除外部订阅。移除与 publish 有明确事务先后顺序，已经交付到 inputs 的事件不因退订而撤销。临时断连、服务关闭和重启均不退订。

self exclusion 按可信 producer_session_id 与接收 session_id 比较，不按 channel_id 比较。Gateway 从认证上下文注入 producer，不接受普通参数伪造身份。State 发默认 waiting completion 时 producer 为该 session 自己，所以不会自唤醒；其他订阅 session 仍收到。

递归完成例：A 请求 B 并取得 W；A 返回 wait_for=(W,)；B 完成时向 W 发事件，成为 A 的 steer 输入。若 B 先完成，事件在 W backlog 等待 A 订阅；若 A 同时等待多个 id，任意一个有事件即可唤醒，剩余订阅仍在。用户前端只读 session.output，不进入这一订阅体系。

## 6. Python 公共契约

以下名字从 `kapy.state` 导出。值对象使用 `@dataclass(frozen=True, slots=True)`；签名中的省略号只表示方案中的方法声明。JSON 对象不含密钥、活连接、取消对象或 Python 实例。

```python
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Literal, Protocol
from uuid import UUID

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type InputMode = Literal["steer", "queue"]
type Cursor = str

@dataclass(frozen=True, slots=True)
class RequestKey:
    scope: str
    key: str

@dataclass(frozen=True, slots=True)
class RunnerState:
    codec: str
    data: JsonObject

@dataclass(frozen=True, slots=True)
class SessionSpec:
    title: str
    machine_ids: tuple[str, ...]
    default_machine_id: str | None
    config: JsonObject
    initial_state: RunnerState

@dataclass(frozen=True, slots=True)
class SessionView:
    id: UUID
    owner_id: str
    parent_session_id: UUID | None
    title: str
    machine_ids: tuple[str, ...]
    default_machine_id: str | None
    config: JsonObject
    status: Literal["waiting", "running", "deleting"]
    run_id: UUID | None
    cursor: Cursor
    created_at: datetime
    updated_at: datetime

@dataclass(frozen=True, slots=True)
class Submission:
    request_id: UUID
    session_id: UUID
    input_id: UUID | None
    waiting_id: UUID
    owner_id: str
    parent_session_id: UUID | None

@dataclass(frozen=True, slots=True)
class CreatedSession:
    session: SessionView
    submission: Submission

@dataclass(frozen=True, slots=True)
class Completion:
    run_id: UUID | None
    outcome: Literal["completed", "failed", "deleted"]
    output: str
    cursor: Cursor
    completed_at: datetime

@dataclass(frozen=True, slots=True)
class SubmissionStatus:
    submission: Submission
    completion: Completion | None

@dataclass(frozen=True, slots=True)
class SessionInput:
    id: UUID
    seq: int
    mode: InputMode
    payload: JsonValue
    event_id: UUID | None

@dataclass(frozen=True, slots=True)
class MessageWrite:
    message_id: UUID
    kind: Literal["model_request", "model_response"]
    text: str
    data: JsonObject

@dataclass(frozen=True, slots=True)
class CheckpointWrite:
    number: int
    state: RunnerState
    messages: tuple[MessageWrite, ...]
    consumed_input_ids: tuple[UUID, ...]

@dataclass(frozen=True, slots=True)
class OutputDelta:
    emission_id: UUID
    message_id: UUID
    kind: Literal["text_delta", "tool_call", "tool_result", "notice"]
    data: JsonValue

@dataclass(frozen=True, slots=True)
class Record:
    cursor: Cursor
    run_id: UUID | None
    attempt: int | None
    message_id: UUID | None
    kind: str
    data: JsonValue
    text: str
    created_at: datetime

@dataclass(frozen=True, slots=True)
class RecordPage:
    items: tuple[Record, ...]
    next_cursor: Cursor
    has_more: bool

@dataclass(frozen=True, slots=True)
class HistoryExportPage:
    items: tuple[Record, ...]
    next_cursor: Cursor
    snapshot_cursor: Cursor
    has_more: bool

@dataclass(frozen=True, slots=True)
class SessionPage:
    items: tuple[SessionView, ...]
    next_after: UUID | None

@dataclass(frozen=True, slots=True)
class EventReceipt:
    request_id: UUID
    event_id: UUID
    waiting_id: UUID
    delivered: int
    pending: bool

@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[JsonValue, ...], ...]
    truncated: bool

class RunContext(Protocol):
    session: SessionView
    run_id: UUID
    attempt: int
    recovered: bool
    inputs: tuple[SessionInput, ...]
    state: RunnerState
    checkpoint_number: int

    async def poll_steer(self, *, limit: int = 64) -> tuple[SessionInput, ...]: ...
    async def emit(self, delta: OutputDelta) -> Cursor: ...
    async def checkpoint(self, write: CheckpointWrite) -> Cursor: ...
    async def read_history(
        self, *, after: Cursor | None = None, limit: int = 200
    ) -> RecordPage: ...

@dataclass(frozen=True, slots=True)
class RunResult:
    output: str
    wait_for: tuple[UUID, ...]
    checkpoint: CheckpointWrite

type SessionRunner = Callable[[RunContext], Awaitable[RunResult]]

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
        self, spec: SessionSpec, *, request_key: RequestKey, owner_id: str,
        parent_session_id: UUID | None = None,
        input: JsonValue = None, mode: InputMode = "queue",
        waiting_id: UUID | None = None,
    ) -> CreatedSession: ...
    async def get_session(self, session_id: UUID) -> SessionView: ...
    async def list_sessions(
        self, *, session_ids: tuple[UUID, ...] | None = None,
        after: UUID | None = None, limit: int = 100,
    ) -> SessionPage: ...
    async def update_session(
        self, session_id: UUID, *, request_key: RequestKey, title: str,
        machine_ids: tuple[str, ...], default_machine_id: str | None,
        config: JsonObject,
    ) -> SessionView: ...
    async def delete_session(
        self, session_id: UUID, *, request_key: RequestKey,
    ) -> bool: ...
    async def submit_input(
        self, session_id: UUID, payload: JsonValue, *, request_key: RequestKey,
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
        self, waiting_id: UUID, payload: JsonValue, *, request_key: RequestKey,
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

`StateError` 及 `NotFound`、`Conflict`、`InvalidArgument`、`UnsafeQuery`、`QueryLimitExceeded`、`ServiceUnavailable` 同样公开导出；State 不直接抛 JSON-RPC 错误。

checkpoint number 在同 run 中严格递增，重复 number 且内容相同返回原 cursor，不同内容报 Conflict；emission_id 同样去重。最终 checkpoint 必须为下一序号。MessageWrite.data 是 Intelligence 对单个 Pydantic AI ModelMessage 的 JSON 编码；State 存原值，Intelligence 在 runner 恢复边界解码。RunContext.state 已含最新可恢复上下文；read_history 提供原始历史分页，不要求每轮加载全部历史。

update 使用完整可修改字段集合以避免 null/未提供歧义；machine/default/config 仅在 waiting 且没有活动 run 时修改，default 必须在 machine_ids 中。当前 run 使用启动时快照。创建前 Gateway 调 Intelligence 的 session 初始化入口取得 skill description 快照和 initial_state；该内容在 create 事务中持久化。create 的 input=null 明确表示不提交初始输入。

RequestKey.scope 由 Gateway 根据已认证主体生成，key 是本次逻辑操作的稳定键，二者 UTF-8 长度各为 1..256 bytes，均不可含密钥。Telegram 可用 scope=`telegram:<bot_numeric_id>`、key=`update:<update_id>:<step>`，不同步骤必须使用不同 key。State 用 (scope,key) 原子去重并生成返回的 request_id UUID；相同键/相同规范化参数返回首次提交的原结果，不再次更新状态。相同键/不同操作、目标或参数报 Conflict。create/update/delete/input/publish 均适用；update/delete 的去重检查早于当前 session 状态检查，防止重放覆盖后来设置或重复删除。执行失败且未提交的操作不占用成功幂等键。

owner_id 与 parent_session_id 由 Gateway 从可信 caller 注入，不是 session.create 的普通外部参数，也不能通过 update 修改。owner_id 是 Gateway 的稳定主体标识；递归创建时 parent_session_id 是调用方 session，目标新 session 由 State 分配。Gateway 可依据这些不可变事实实现 self/created-child 权限；State 不推导授权，不把 owner 相同自动解释为 session token 可访问全部 session。Submission 同时保留此来源，使 Gateway 在 session 删除后仍能对 receipt 观察授权。caller token、目标 session_id、request key scope 分别传递，互不替代。

## 7. JSON-RPC 参数与结果

下表所有方法 params 均为 object，省略字段采用 Python 默认值；变更参数 `request_key` 是调用方提供的稳定字符串，Gateway 注入 scope 后构造 Python RequestKey；返回的 `request_id` 是 State 生成的 UUID，用于 receipt 观察。JSON-RPC envelope.id 不承担幂等责任。UUID 用字符串，datetime 用 UTC RFC3339，tuple 用 array，cursor 为绑定 session 的不透明字符串。表中类型名称表示第 6 节值对象逐字段 JSON 化，没有额外包裹。

| Method | Params | Result |
| --- | --- | --- |
| `session.create` | `{request_key,title,machine_ids,default_machine_id,config,input?,mode?,waiting_id?}`；initial_state、owner_id、parent_session_id 由 Gateway 可信注入 | `CreatedSession` |
| `session.get` | `{session_id}` | `SessionView` |
| `session.list` | `{after?,limit?}`；Gateway 内部传授权 session_ids | `SessionPage` |
| `session.update` | `{session_id,request_key,title,machine_ids,default_machine_id,config}` | `SessionView` |
| `session.delete` | `{session_id,request_key}` | `{deleted: bool}` |
| `session.input` | `{session_id,request_key,payload,mode?,waiting_id?}` | `Submission` |
| `session.output` | `{session_id,after?,limit?,wait_seconds?}` | `RecordPage` |
| `session.wait` | `{session_id,request_id,wait_seconds?}`；只观察 create/input receipt | `SubmissionStatus` |
| `event.publish` | `{waiting_id,request_key,payload,mode?}`；producer_session_id 来自 Gateway 认证上下文 | `EventReceipt` |
| `history.read` | `{session_id,after?,limit?}` | `RecordPage` |
| `history.search` | `{session_id,query,mode?,after?,limit?}` | `RecordPage` |
| `history.query` | `{session_id,sql,params?,limit?}` | `QueryResult` |
| `history.export` | `{session_id,after?,snapshot?,limit?}` | `HistoryExportPage` |

订阅由 runner 的 wait tool 返回 wait_for，经 State 提交；不增加前端 event.subscribe/event.ack 接口。普通 CLI/Telegram 等待某次请求用 session.wait 的非消费 long-poll，读取输出用 session.output；运行在 session 内的递归 CLI 把 waiting_id 交给 wait tool。waiting_id 是可反复发布的频道名，request_id 标识一次请求，cursor 标识 session 记录位置，三者不可互换。

completion payload 精确为 `{type:"session.waiting",session_id,run_id,request_ids:[UUID],outcome:"completed"|"failed"|"deleted",output:string,cursor:Cursor}`；空创建的 run_id 为 null。event 输入保留 `{type:"event",event_id,waiting_id,producer_session_id,payload}`，外层 SessionInput.mode 决定调度。submitted payload 不改变用户原值。

重复 RequestKey+相同操作/参数返回首次结果；同键不同参数报 Conflict。create 参数指纹包含用户请求字段及可信 owner/parent 来源，不含 Gateway 衍生的 initial_state；重试采用首次成功持久化的 skill/runner 快照。Telegram inbox 必须固定逻辑步骤的请求参数和 key，不能重放时改用后来 saved config。State 的幂等记录无需与 Gateway inbox 跨包同事务：State 提交后 Gateway 崩溃会以同键重试，再完成自己的 binding/inbox 提交。输出 record.kind 为 input、text_delta、tool_call、tool_result、notice、model_request、model_response、final、waiting、error、interrupted。final.data 为 `{output:string}`；waiting.data 为上述 completion payload；输入记录 data 为原输入或事件 envelope。

Gateway 负责把标准无效参数映射 -32602；建议 State 错误映射 NotFound=-32004、Conflict=-32009、UnsafeQuery=-32020、QueryLimitExceeded=-32021、ServiceUnavailable=-32030。错误 data 使用 `{kind,retryable}`，不返回原始 SQL、DSN 或数据库内部异常。认证及 channel 发布/订阅权限由 Gateway/Intelligence 的可信边界处理，State 的 UUID 不是授权凭据。

## 8. 输出、回放与历史 SQL

`session.output(after=None)` 从最初记录开始读取，之后以 next_cursor 续读，排空后同一接口 wait_seconds>0 进入实时 long-poll；先注册本地唤醒、再读 PostgreSQL，睡醒后重读，且最多 1 秒重查，避免检查与等待之间的丢唤醒。空页保留已扫描 cursor；断开只取消读者，不修改 session 或订阅。

输出由 PostgreSQL 分页承载，不为每个前端保存无限内存缓冲。一次 emit 对应一次短事务，返回意味着已持久化；runner 可把连续文本合成一个不超过 16KiB 的 delta。完整 model_response 使用 message_id 替换其 delta 投影，final 表示该 run 的最终结果。前端按 message_id 维护展示，不把完整消息再追加一份。history.read/search 只返回 input/model_request/model_response/final/waiting/error，使用相同顺序空间但不作为实时切换入口；可靠回放到实时统一走 session.output。

history.export 导出同一历史子集。首个请求捕获已提交最高 seq 作为 snapshot_cursor，后续请求必须带回该 snapshot 及 next_cursor，只读取 `(after,snapshot]`，末页 next_cursor 到 snapshot。传 after 却未传 snapshot 时拒绝请求，避免分页中悄悄改变边界。records 只追加，所以无需跨 RPC 保持数据库事务即可得到稳定有限快照。after/snapshot 都验证绑定的 session；删除期间无法继续导出时明确报 NotFound。Gateway 将页面序列化为流式 NDJSON/下载文件，State 不一次性拼接全部历史，也不持有文件或 HTTP response。每页仍受 200 行/1MiB 限制。

历史 SQL 提供 PostgreSQL SELECT 的明确子集，只能读取逻辑关系 `history(seq,run_id,kind,message_id,text,data,created_at)`。SQLGlot 负责解析，State 安全编译器对每个节点及其参数槽做完整白名单，并从已验证节点生成 SQL；不执行调用者原字符串，不采用函数黑名单。[SQLGlot AST API](https://sqlglot.com/sqlglot.html)

允许投影、别名、WHERE、AND/OR/NOT、比较、IS NULL、LIKE/ILIKE、IN、EXISTS、ORDER BY、GROUP BY、HAVING、LIMIT，以及受限 INNER/LEFT JOIN、FROM/scalar 子查询；每个子查询同样递归检查，只能引用 history 或合法局部别名。函数仅允许 `count/min/max/lower/length/coalesce` 的明确参数形状，生成时限定 pg_catalog 内建函数；COALESCE 按 SQL 特殊语法处理。值和 :name 参数全部变为 driver binds；标识符用 Identifier 生成。默认列类型均为已知内建类型，不允许用户指定类型、collation 或 operator。

拒绝多语句、用户 WITH/UNION、DDL/DML、SELECT INTO、锁子句、系统/物理/schema-qualified 表、table function、任意未许可函数、cast、窗口、LATERAL、递归、未许可操作符及未知 AST 参数。解析成功不等于安全；任何遗漏节点或槽默认拒绝。不允许借别名把内部关系名带入 SQL。

编译器在外层加入不可由用户命名或覆盖的 MATERIALIZED CTE，仅使用服务绑定的 session_id 筛选 records 的历史子集；所有逻辑 history 引用，包括 join 和深层子查询，改写成这一份已筛选关系。MATERIALIZED 防止用户表达式被下推到过滤前。查询事务为 READ ONLY，search_path 固定 pg_catalog，关系名均安全限定，statement_timeout=2s、lock_timeout=250ms。RLS 不作为隔离成立的前提。[PostgreSQL CTE materialization](https://www.postgresql.org/docs/17/queries-with.html)

SQL 输入至多 16KiB、AST 256 节点、嵌套 4 层、关系引用 4 处；结果最多 200 行、1MiB，使用 server cursor 限量取回并在超限时取消。行数超限返回前 limit 行和 truncated=true；字节超限返回 QueryLimitExceeded，避免一行超大值压垮 RPC。禁止 SQL 文本设置任何这些上限。

substring 使用 `strpos(text, %s)>0`，对用户原值做参数化字面子串匹配，百分号和下划线没有隐含通配意义。fulltext 在写入时对 text 做 NFKC+casefold：按 Unicode 字母/数字及其组合附加符组成 token；中日韩字符块额外拆成单字符及连续双字符 token，写入内置 simple tsvector，建部分 GIN 索引；查询采用相同规则，各空白分隔关键词 AND，CJK 关键词用 bigram 召回再以规范化 substring 复核，单字符关键词使用 unigram。结果按 seq 返回，支持稳定增量分页；不引入相关度分页游标。

该方案支持 Unicode 多语言关键词和 CJK 子串召回，不声称提供各语言词干、语义检索或完善语言学分词。现场查询证实原生 simple 把“你好世界”保留为整词，因此不能直接用它替代 CJK 拆词。GIN 用于全文候选集，不为 substring 首版引入扩展。[PostgreSQL 文本搜索索引](https://www.postgresql.org/docs/17/textsearch-indexes.html)、[文本搜索函数](https://www.postgresql.org/docs/17/functions-textsearch.html)

## 9. 实现分界与跨模块要求

State 代码按 `contracts.py`、`store.py`、`service.py`、`history.py` 与 migrations 组织；不另外引入通用 repository/event-bus 抽象。既有目录为空，这些均为新增代码，正式实现和分阶段子代理审查由本 senior 在批准后负责。

Intelligence 按 SessionRunner 契约实现 runner、codec、创建时 skill 快照与等待工具；在模型/工具边界 poll_steer，检查点确认输入，返回 RunResult。模型上下文压缩和多模态错误修复完全归 Intelligence；状态读出不做反序列化模型校验。RunContext 是可信能力对象，不把 SessionService/数据库连接交给模型。

Gateway 在启动时先 migrate，再 lifespan 管理 SessionService；保证 machine/default 合法和身份授权，转发确定的参数/结果；为 session.create 取得 initial_state，为 session.list 提供授权范围，不把 None 当作用户可请求的全量范围。session config 只存模型名称、阈值等非秘密设置；API key 保持在装配层。频道权限在调用 State 前处理，runner wait_for 也需通过 Intelligence 注入的授权检查。Gateway 的 durable inbox、chat/topic binding、saved config、output cursor 和认证/grant 表由 Gateway 自包拥有；总设计师的装配入口依次调用各 owner 的迁移。`kapy.state.migrate` 只管理 State schema，不反向导入 Gateway，也不执行文档中的任意 DDL。

Execution 无需依赖 State。递归 CLI 需要保留 request_key、返回的 request_id 和等待频道，并把 session token 交 Gateway；恢复外部工具时应允许按现有 process/request id 查询已知状态，不能因控制连接重连自动重复命令。Gateway/Execution 对 JSON-RPC frame 上限至少容纳本方案 1MiB 页面加 envelope，或装配时统一下调 State 页面字节上限。

单个输入、事件或 model message 上限建议 256KiB JSON，单条 delta 16KiB，checkpoint 4MiB，wait_for 至多 128 个，page limit≤200，long-poll≤30s；超限在变更前明确拒绝。媒体通过引用进入历史，不把大型二进制直接编码到 records。pending/backlog 和历史不按内存窗口丢弃，也不在首版自动设置 TTL。

总设计师需要统一批准的契约取舍为：本方案精确 Python/RPC 导出，包括 RequestKey、不可变 owner/parent 来源、非消费 session.wait 和分页 history.export；直接 session.input 与 MPMC event.publish 的区分；sticky wait_for 到下一次返回时替换；受限 SELECT 而非任意 PostgreSQL；内置 Unicode/CJK 全文策略；外部工具结果未知时不自动重发。这些均按上述具体默认方案提交，没有在实现中留给临时判断的空白。
