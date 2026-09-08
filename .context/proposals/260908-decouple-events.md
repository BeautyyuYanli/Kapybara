将异步事件系统提取为独立的 `kapy.events` 包，由 `SessionService` 通过明确接口集成。事件组件负责持久事件、订阅、广播和积压投递；session 负责输入模式、运行状态、默认通道和完成通知。两者借用同一个 PostgreSQL 事务，保留进入 waiting 与通知投递的原子性。

依赖方向为 `Gateway → State → Events`，Agent 继续通过 State 的 `RunContext` / `RunResult` 工作。`kapy.events` 不导入 State、Agent 或 Gateway，也不查询它们的表。

| 所有者 | 数据与行为 |
| --- | --- |
| Events | 事件 ID、通道 ID、不透明的发布者/订阅者 UUID、JSON payload、持续订阅、pending/delivered 状态 |
| State | session/run/input/request/history/checkpoint、steer/queue、完成通知对象、事件到 session 输入的转换 |
| Gateway | 调用方身份、通道创建与授权、从请求 ID 分配完成通道、外部 RPC |

`waiting_id` 是 session 接口中对事件通道 ID 的称呼。Events 统一使用 `channel_id`；发布者与订阅者共用 UUID 身份空间，以便排除自投递。Events 不赋予任何 UUID 默认通道或 session 含义。

公开接口从 `kapy.events` 导出如下。本次只实现 PostgreSQL 后端；`EventBus` 表达事务接口与注入边界，`PostgresEventBus` 完整实现其两个方法。

```python
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type Connection = AsyncConnection[dict[str, Any]]

@dataclass(frozen=True, slots=True)
class Event:
    id: UUID
    channel_id: UUID
    producer_id: UUID | None
    payload: JsonValue

type Deliver = Callable[[Connection, UUID, Event], Awaitable[bool]]

class EventBus(Protocol):
    async def publish(
        self,
        conn: Connection,
        event: Event,
        *,
        deliver: Deliver,
    ) -> int: ...

    async def replace_subscriptions(
        self,
        conn: Connection,
        *,
        subscriber_id: UUID,
        channel_ids: tuple[UUID, ...],
        deliver: Deliver,
    ) -> None: ...

class PostgresEventBus(EventBus):
    def __init__(self, *, schema: str) -> None: ...

async def migrate(conn: Connection, *, schema: str) -> None: ...
```

`publish` 保存新事件，按该通道的事件顺序尝试投递积压和本次事件，返回本次事件实际交接的订阅者数量。返回零表示本次事件仍为 pending。一个事件交接给当时所有合格订阅者后成为 delivered；以后才订阅的接收者不补收它。只有发布者自己订阅时仍保持 pending。

`replace_subscriptions` 对一个订阅者整体替换通道集合，重复通道按集合处理，然后尝试投递这些通道的 pending 事件。空集合表示退订全部通道，包括删除订阅者时的清理。订阅不会随着一次投递自动消失；退订不撤回已交接的消息。每通道按持久的 `ordinal` 处理事件，不承诺不同通道之间的全局顺序。

`Deliver` 的第二个参数是接收者 UUID。回调返回 `True` 表示已在传入事务中持久接收该事件，返回 `False` 表示该接收者当前不具备接收资格，例如 session 正在删除。异常必须向上传播，使事件保存、所有接收者写入和 delivered 标记一起撤销。回调只执行同一连接上的数据库操作，不提交事务，不调用模型、执行机或外部消息服务，也不递归调用事件接口。

同一事件存储命名空间的调用使用同一种投递适配器；适配器负责解释该命名空间内的接收者 UUID。当前应用只接入 session 适配器，事件内核不增加订阅者类型注册表。

事件组件借用已经开启的 READ COMMITTED 写事务，不创建连接池、控制进程租约、后台任务或 Valkey 客户端，也不自行提交。`PostgresEventBus` 每次写操作先取得本 schema 专用的事务 advisory lock，使发布与订阅变更串行化，包括首次发布与首次订阅的并发情况。State 调用顺序固定为现有 `Store.write()` 的租约/写锁，然后事件锁；不得绕过 State 直接投递到 session 表。

`Event.id` 由调用方生成，事件表主键阻止重复 ID 写入。请求级幂等仍归调用方：State 保留现有 `requests` 表、指纹和回执，在同一事务内检查 `request_id` 后调用事件接口并保存结果。重试返回原回执，不再次发布。内部完成事件与请求 completion 同事务保存，无需新增事件请求表或另一套重试协议。

State 新增 `state/events.py`，保存 session 专用的消息编码辅助函数；投递回调直接绑定在 `SessionService` 上，复用现有输入写入能力。Events 与 State 共用 Events 声明的 JSON 类型，State 继续导出当前的 `JsonValue` / `JsonObject` 名称。session 事件在内核中的不透明 payload 为：

```python
{"mode": "steer", "payload": original_payload}
```

`mode` 可以是 steer 或 queue，其合法值与默认值由 State 解释，Events 仅保存 JSON。绑定回调的签名为：

```python
class SessionService:
    async def _accept_event(
        self,
        conn: Connection,
        subscriber_id: UUID,
        event: Event,
    ) -> bool: ...
```

State 构造事件时生成 UUID，并在发布前沿用现有 payload 和历史记录大小校验，保证无人订阅时也不会接受将来无法写入输入记录的事件。`_accept_event` 查询接收 session 是否存在且不处于 deleting；合格时解析 mode，将事件转换成现有 envelope，再通过 `_insert_input` 保存 records 和 inputs，返回 `True`。输入保存 `event_id`，保留 `(event_id, session_id)` 唯一约束。

Agent 和历史读取看到的 envelope 保持如下形状，内核中的 mode 包装不会泄漏到业务 payload 中：

```json
{
  "type": "event",
  "event_id": "<event UUID>",
  "waiting_id": "<channel UUID>",
  "producer_session_id": null,
  "payload": null
}
```

上例的发布者也可为 UUID 字符串，payload 为原始 JSON 值。回调只完成持久交接；reserved/consumed 和 checkpoint 继续由 session 管理。Events 不判断模型是否处理完消息。

SessionService 增加一个必传的构造依赖，其余构造参数保持原有含义：

```python
SessionService(
    *,
    database_url: str,
    valkey_url: str,
    runner: SessionRunner,
    events: EventBus,
    schema: str = "kapy_state",
    namespace: str = "kapy_state",
)
```

Gateway 装配 `PostgresEventBus(schema=config.database_schema)` 并传入 SessionService。事件组件的构造没有 I/O，SessionService 不负责关闭它。State 仍拥有连接池、控制进程租约、调度器、Valkey 提示和最多一秒一次的持久工作扫描。

Session 的调用位置明确如下：

1. 创建时，在创建 session 的事务里调用 `replace_subscriptions`，建立 `{session_id}` 默认订阅，并尝试交接该通道的积压事件。空创建仍立即生成创建请求的完成通知；带初始输入的创建等实际消费它的 run 完成。
2. `publish_event` 保留现有公开签名与 EventReceipt。State 校验业务参数、检查请求幂等、构造 Event，再调用 `publish`。接收数量转成现有 `delivered` 与 `pending` 字段。普通 `submit_input` 继续直接写目标 session 输入，其 waiting_id 仍只指定完成通知通道。
3. 正常结束时，在现有 `_finish` 事务中保存最终 checkpoint 和输出、切换 waiting、调用 `replace_subscriptions` 设置 `{session_id} ∪ wait_for`，再为本轮已消费请求发布完成事件。默认通道每批最多包含 64 个 request_id；独立请求通道携带该请求的单个 ID。完成通知的 payload、去重规则和 `steer` 模式由 State 负责。
4. 失败时，State 按现有已接手输入范围发布 failed 完成事件，保留原订阅。取消和进程中断保留恢复状态，不伪造完成。最终删除事务中先将订阅集合置空，再发布未完成请求的 deleted 通知并删除 session；处于 deleting 但尚未完成清理的接收者由投递适配器排除。
5. 提交事务后由 State 调用原有 `_signal()`。投递产生的输入遵循现有 steer/queue 调度。Runner 的 `wait`、`RunResult.wait_for` 和 Gateway 的订阅权限校验保持现有调用方式。

这样，父 session 返回等待通道，子 session 发布完成事件，父 session 收到 steer 输入的递归过程全部经过新的事件接口。前端输出读取和 `wait_submission` 继续读取 session 记录，不消费事件队列。

事件持久模型采用两张表，继续使用当前 PostgreSQL 数据库和 schema：

| 表 | 字段与约束 |
| --- | --- |
| `events` | `id UUID PRIMARY KEY`、`ordinal BIGINT` 自动序号、`channel_id UUID`、`producer_id UUID NULL`、`payload JSONB`、`state pending/delivered`、`created_at`；保留通道 pending 顺序索引 |
| `subscriptions` | `channel_id UUID`、`subscriber_id UUID`；联合主键，保留 subscriber_id 查询索引；无 session 外键 |

新增 State 的 `002_extract_events.sql` 处理现有结构：将 `subscriptions.session_id` 改为 `subscriber_id` 并移除 session 外键，将 `events.producer_session_id` 改为 `producer_id`，把每条事件的 mode 和原 payload 合并为上述 JSON 包装后移除 mode 列。事件 ID、ordinal、投递状态、订阅集合和既有 inputs.event_id 均保留。原 `001_initial.sql` 与已保存的迁移校验值不改写。

`kapy.events.migrate(conn, schema=...)` 拥有事件表后续 DDL 与独立的 `event_schema_migrations` 记录。初始 DDL 使用 `CREATE TABLE IF NOT EXISTS`，只支持两条明确路径：全新 schema 直接创建事件表；当前 Kapy schema 先由 State 的 002 完成转换，再执行相同 DDL 并登记 Events 初始迁移。采用顺序 SQL 与校验值记录，不增加通用结构识别或自动修复机制。应用先完成 State 迁移，再调用 Events 迁移；独立使用 Events 无需创建 session 表。schema 标识符经校验并安全引用，事件 SQL 不依赖调用方的 search_path。

实现涉及新增 `kapy/events/{__init__,contracts,postgres}.py`、`kapy/events/migrations/001_initial.sql` 和 `kapy/state/events.py`；Events 的迁移入口放在 postgres 模块。从 `state/service.py` 移出事件存储与广播 SQL，保留 session 完成通知的构造逻辑。同步修改 State 构造依赖、Gateway 装配和迁移调用，以及直接构造 SessionService 的调用点；更新 `docs/architecture.md`、`docs/contracts.md`、`docs/state.md` 记录新接口与数据所有权。使用已有 psycopg 和 PostgreSQL，不新增依赖或运行服务。
