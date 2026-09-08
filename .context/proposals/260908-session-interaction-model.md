Session 采用创建时确定的交互模式：普通模式通过 `wait_for` 或正常文本结束；显式回复模式通过 `wait_for` 或 `reply_to` 结束。输入消费、等待外部结果、回复输入分别记录。进入 waiting 本身不结算输入。

本次在现有 State、Runner、Gateway 中实现交互模型。事件能力继续留在 State，使用现有 PostgreSQL 事务和调度器。删除默认 channel、自动自订阅、默认完成广播和 MPMC 投递规则。

| 创建配置 `output_mode` | 模型可见输入 | Pydantic AI 出口 | 回复选择 |
| --- | --- | --- | --- |
| `text`，默认 | 原始输入内容 | `str`、`wait_for` | 正常文本结束时自动回复全部已交给模型且尚未回复的输入 |
| `reply_to` | 原始内容及该输入的 `being_waited_id` | `wait_for`、`reply_to` | 模型显式列出本次回复的 being_waited_id |

`output_mode` 存在 `session.config` 中，仅创建时设置，创建后不可修改。CLI 增加 `session create --output-mode text|reply_to`，Gateway 创建参数和配置检查接受该字段。普通模式不注入 being_waited_id，不注册 reply_to，也不追加任何 reply_to 的提示词；内部仍为每条直接输入分配回复通道。

一条 `session.input` 创建一个一次性回复通道。调用方回执中的 `Submission.waiting_id` 与接收方看到的 `being_waited_id` 是同一个 UUID，分别表达“等待这个回复”和“回复这条输入”。每个新请求分配新通道；相同 request_id、相同参数重试返回原回执。取消 create/input 的自选 `waiting_id` 参数及 `--waiting-id`，禁止不同输入复用同一个回复通道。

Session 的输入只有两类：

| 来源 | 入队方式 | 回复义务 |
| --- | --- | --- |
| `session.input`，包括创建时携带的初始输入 | 指定 steer/queue，沿用现有顺序和 checkpoint 消费机制 | 有独立 being_waited_id，读入模型后成为待回复输入 |
| `waiting` 结果交接 | 默认 steer，事件发布显式指定 queue 时保留该模式 | 没有新的 being_waited_id，不再自动产生“回复的回复” |

State 的 `SessionInput` 保留 id、seq、mode、payload、event_id，并增加 `being_waited_id: UUID | None`。payload 保持原始业务值；ID 来源于 State 的请求关联，不从用户提交的 JSON 或文字中解析。

显式模式将直接输入编码为模型可见的结构：

```json
{
  "type": "session_input",
  "being_waited_id": "<reply channel UUID>",
  "payload": "原始输入内容，也可以是其他 JSON 值"
}
```

普通模式沿用原始输入的呈现方式。两种模式都能接收 waiting 结果；该结果携带所等待的通道 ID 和生产方的完整 output，以便与之前的调用关联，外层没有新的 being_waited_id。

State 保留 `pending → reserved → consumed` 输入生命周期。consumed 只表示已随 checkpoint 交给模型，不表示已回复；对应 request 的 completion 为空时，回复义务跨 run 保留。回复后既有输入仍保留在历史中，不重新入队。

Pydantic AI 的结构化输出类型从 State contracts 导出：

```python
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

@dataclass(frozen=True, slots=True)
class WaitFor:
    waiting_ids: tuple[UUID, ...]
    kind: Literal["wait_for"] = "wait_for"

@dataclass(frozen=True, slots=True)
class ReplyTo:
    being_waited_ids: tuple[UUID, ...]
    payload: str
    kind: Literal["reply_to"] = "reply_to"

type SessionOutput = str | WaitFor | ReplyTo

@dataclass(frozen=True, slots=True)
class RunResult:
    output: SessionOutput
    checkpoint: CheckpointWrite
```

模型侧的两个输出函数都只有一个 ID 数组参数：

```python
async def wait_for(ids: list[UUID]) -> WaitFor: ...

async def reply_to(ids: list[UUID]) -> ReplyTo:
    await validate_reply_targets(ids)
    return ReplyTo(
        being_waited_ids=tuple(ids),
        payload=latest_assistant_text(),
    )
```

`ReplyTo.payload` 是返回 DTO 的字段，不是输出函数参数。模型不填写 payload、正文副本或消息 ID。输出函数在执行时补齐 DTO 并返回给 Pydantic AI；`AgentRunResult.output` 就是这个完整 ReplyTo 对象。

注册方式按 session 的创建配置选择：

```python
wait_output = ToolOutput(wait_for, name="wait_for", sequential=True)

if output_mode == "text":
    output_type = [str, wait_output]
else:
    output_type = [
        wait_output,
        ToolOutput(reply_to, name="reply_to", sequential=True),
    ]

agent = Agent(
    model,
    output_type=output_type,
    tools=machine_tools,
    end_strategy="exhaustive",
    capabilities=[boundaries],
)
```

输出函数放在 `output_type`，不重复注册为普通工具。显式模式不包含 str 或 None 出口；只有文本、空响应或未通过参数检查的调用都不能作为成功结果。使用 Pydantic 参数约束与 ModelRetry 要求模型纠正，超出框架重试预算进入既有运行失败路径。Pydantic AI 支持输出函数的参数检查、ModelRetry 和将函数返回值作为 run output。[输出函数契约](https://pydantic.dev/docs/ai/core-concepts/output/#output-functions)

保留 exhaustive 工具批次处理。输出函数仅构造候选结果，不能立即发布通道结果或改变订阅。同一响应包含多个输出调用时，采用框架选中的首个有效结果；其余候选不产生业务副作用。普通工具重试、steer 注入及恢复路径继续服从已有批次完成规则。[当前锁定版本的出口选择规则](https://github.com/pydantic/pydantic-ai/blob/v2.40.0/pydantic_ai_slim/pydantic_ai/_agent_graph.py)

`latest_assistant_text()` 取当前 run 中最近一个已完整记录、包含非空可见 TextPart 的 ModelResponse；同一响应内的 TextPart 按顺序合并，同一响应内同时出现正文和 reply_to 时也适用。工具返回、thinking、未完成流片段以及上一个 run 的文本不作为候选。没有合格正文时输出函数抛 ModelRetry，要求先输出正文。补齐后的 ReplyTo 是不可变输出快照；后续不重新查找“最新消息”。

后续链路统一处理完整 `AgentRunResult.output`。Runner 将其直接放入 RunResult，并通过同一套结构化 output 序列化写入 checkpoint、final 记录和 cycle 的 output。State 只用输出类型和目标 ID 决定路由，将完整 output 作为通道结果交接。历史压缩保留完整 output，不单独提取 ReplyTo.payload，不维护并行的“回复正文”字段，也不从最终 DTO 反向拼装模型消息。

普通 str、WaitFor 和 ReplyTo 使用同一个 Pydantic TypeAdapter 完成 JSON 序列化与恢复。第一级压缩仍省略旧工具结果；第二级保留输入与完整 output 的通用序列化表示，因此 ReplyTo 的 ID 和 payload 都作为正常 output 一起保留。未回复输入及其地址属于未完成上下文，不随已关闭 cycle 丢弃；回复完成后才能参与普通历史压缩。前端继续展示模型文本消息和流式文本，结构化 final 记录用于确定运行边界，不把 DTO 的字符串表示发送给用户。

`wait_for` 接受 1–128 个不同通道 ID。它替换当前活动等待集合，不结算任何直接输入。任意一个通道的结果可以唤醒 session；同一事务里已经 ready 的多个通道均交接一次，其余尚未得到结果的等待继续有效。直接输入始终可以唤醒 session，不依赖等待集合。已经 delivered 的通道不能再次等待。

`reply_to` 接受最多 128 个不同 being_waited_id，只能引用当前 session 已交给模型、尚未回复的输入，包括以前 run 留下的输入。未知 ID、其他 session 的 ID、尚未消费的 queue 输入以及已经结算的 ID，均在输出函数内转成 ModelRetry，并在 State 最终写事务中再次检查。目标列表整体有效才提交，不部分成功。空数组仅在没有已读未回复输入时有效，表示保存本轮正常结构化输出但不向任何输入通道回复。

正常文本结束自动选中当前 session 全部已读未回复输入，包括此前 wait_for 留下的输入；其结果仍是框架原始的 str output，不转换成模型不可见的 ReplyTo。文本或 reply_to 结束会清除当前活动等待集合。显式 reply_to 只结算选中输入并结束本轮；未选中的已读输入保持未回复，后续由 session.input 或 waiting 结果继续推动。需要暂停处理去等外部结果时，模型应选择 wait_for。

Runner 的提示词由公共任务说明和对应模式的交互说明组成。创建时保存业务 instructions 与 skill 快照，协议说明在每次运行时按不可变 output_mode 组装，避免把旧协议固化在不可替换的长字符串里。

普通模式的协议说明表达：

> 你可以输出文本完成这一轮，或调用 wait_for 等待列出的结果。wait_for 的列表不能为空。等待不会回复当前请求；收到结果后继续完成工作，再输出最终文本。新输入始终可以继续当前会话。

显式模式的协议说明表达：

> 直接输入附有 being_waited_id，表示这条输入正在等待你的回复。先输出完整回复正文，再调用 reply_to，参数只填写这段正文要回复的 ID 列表。运行时会将最近一条模型文本补入输出对象；不要在参数中重复正文。一个 ID 只能回复一次，也可以用同一段正文回复多个输入。需要等待其他结果时调用 wait_for；等待不会自动回复任何输入。waiting 结果不是新的待回复请求。仅输出文字不会结束这一轮。

显式模式还在每次模型请求前呈现当前未回复地址清单，防止跨 run 或压缩后丢失回复地址。清单来自 State 的持久关联，不信任模型自行记忆或用户 payload 中的伪造 ID。正文继续来自既有输入历史，不通过清单重复装载。普通模式不构造或注入该清单。

RunContext 增加一个只读接口供显式模式使用：

```python
@dataclass(frozen=True, slots=True)
class ReplyAddressPage:
    being_waited_ids: tuple[UUID, ...]
    next_after: int | None

async def unreplied_addresses(
    *, after: int = 0, limit: int = 64,
) -> ReplyAddressPage: ...
```

查询严格限定当前 session、已消费输入和空 completion，按输入 seq 分页，沿用 State 的编码大小限制。新的本轮输入先按现有 checkpoint 流程确认消费，再进入此清单。输出函数的 validate_reply_targets 根据本次模型请求使用的地址集合检查参数，无需另增 State 查询接口；State 在最终事务中根据持久记录复核并结算，Runner 只提供完整 output 和最终 checkpoint。

通道是一次性、一对一的结果交接，状态为：

```text
open → ready → delivered
```

一个通道绑定唯一生产方和至多一个接收 session。回复通道的生产方就是收到该输入的 session，接收方来自调用方身份；前端调用者通过请求回执观察结果，不占用 agent 投递队列。已有授权下首次绑定接收 session 后不可转给另一 session，取消活动等待也不释放该绑定。producer 与 receiver 的鉴权仍由 Gateway 负责，State 不引入用户身份或通用 ACL 模型。

open 表示尚无结果；ready 表示结果已经提交但尚未交接；delivered 表示已在事务中写入唯一接收 session 的 inputs。生产方先回复、接收方后 wait_for 时，结果保留在 ready 并在建立等待时交接。交接后的通道保持终态记录，不能再发布、再消费或换接收者。底层相同请求的重试返回既有结果，不能产生第二次逻辑投递。

`reply_to([A, B])` 分别完成 A、B 两条一对一通道，交接的是同一个完整 ReplyTo output，不把单条通道广播给多人。waiting 产生的输入使用通道 ID 作为 event_id，输入表以 UNIQUE(event_id) 保证唯一入队；无需再为单次通道结果生成第二个事件身份，也无需在通道上重复保存 input ID。移除 producer=self 的投递过滤规则，资格由明确端点与通道状态决定。

State 使用 `waiting_channels` 保存通道状态，取代原先支持重复事件的 events 与多订阅者 subscriptions：

| 字段 | 含义 |
| --- | --- |
| `id UUID PRIMARY KEY` | waiting_id / being_waited_id |
| `request_id UUID UNIQUE NULL` | 关联的直接输入请求；独立外部事件通道可为空 |
| `producer_session_id UUID NULL` | 输入接收 session；外部生产者身份由 Gateway 管理 |
| `receiver_session_id UUID NULL` | 唯一 agent 接收方，绑定后不可更换 |
| `active BOOLEAN` | 接收方当前是否正在等待此通道 |
| `state` | open、ready 或 delivered |
| `output JSONB NULL` | 框架完整 output 的通用 JSON 表示 |
| `mode` | 交接为输入时的 steer/queue；回复结果使用 steer |

requests 继续保存幂等指纹及控制回执。回复结果以通道的 output 为唯一持久值，`wait_submission` 通过关联通道返回结果及终态，不重复保存一份裸文本。通道另存发布 run、时间与 outcome，支持 completed、failed、deleted；这些字段是交接元数据，不冒充模型 output。

独立外部事件沿用现有受授权的通道登记与 event.publish 调用路径，收紧为一个生产方、一个接收方和一次发布。输入关联通道只能由 State 的输出结算或异常终结路径发布，普通 event.publish 不能绕过 reply_to/正常输出结算规则。Gateway 将多方 channel grants 收敛到明确端点，不再开放广播授权。

State 的结束事务按输出类型执行：

1. 共同部分校验 run/attempt，提交最终 checkpoint、完整 output 和运行结束记录；单纯记录 waiting 不触发通知。
2. WaitFor 更新活动等待集合，交接已有 ready 结果，保留全部未回复输入。即使有未回复输入，也不会仅因此自动启动下一轮；直接输入或结果交接产生的 pending 输入可以启动它。
3. str 结算所有已读未回复输入；ReplyTo 结算指定输入。完整 output、请求结果关联、通道状态和接收方 inputs 在同一个事务中提交。
4. 文本或 reply_to 结束后等待新输入。未选中的输入保留回复义务，不触发额外的自动 run，也不重放已消费消息。
5. 提交后使用现有 `_signal()` 通知调度器；Valkey 仍只是提示，PostgreSQL 是持久真源。没有默认通道，没有自动向 session_id 发布的结果，也没有默认自订阅。

不可恢复的 runner 失败以 failed 终结当前 session 已接手但未回复的输入，包括之前等待阶段留下的输入；尚未接手的 queue 保留给后续处理。session 删除以 deleted 终结全部未回复输入，包括排队中的输入。取消、进程中断和租约失效保留恢复状态，不产生伪回复。系统错误终态属于控制面的失败处理，不是模型的第三个正常出口。

输出函数本身不做不可撤销投递。Runner 将框架选中的完整 output 保存到 pending_final；恢复直接使用这个 output 对象，不能重新取最新文本或拼接 ReplyTo。若尚未得到框架最终结果，需要恢复未完成输出函数，则按照其原始模型响应重建候选，先恢复旧工具批次再注入新输入。新的 steer 若使最终候选失效，应重新请求模型输出，不能把旧正文隐式回复给新输入。

创建空 session 不创建输入回复通道，`CreatedSession.submission` 为 None；只有真实初始输入才返回 Submission。空创建仍有普通创建请求的幂等回执，不伪造空模型输出。新增 SQL 迁移补齐 output_mode、通道结构和回复关联，原迁移文件不改写。既有 session 缺少 output_mode 时按 text 解释。

迁移保留已完成请求的历史结果，不重发旧通知；删除默认通道及自订阅对应的活动关系。仍在使用的非默认旧通道，只有满足单输入、单生产方、单接收方且至多一个结果时才转换成一次性通道；遇到旧通道复用或广播关系时事务明确失败，不能静默选择某个请求或接收者。旧 Runner 快照中的自动结束候选只允许恢复为普通文本模式的结果；完整历史和媒体引用保持可读。

实现修改 State contracts、service 和迁移，Runner 的 output_type、输出函数、prompt 组装、pending_final 与通用 output 序列化，及 compression 对结构化 output 和未回复输入的保留逻辑。Gateway/CLI 增加创建配置并收紧通道端点与请求参数；Telegram 继续从文本消息展示内容，适配结构化运行结束记录。同步更新 `docs/contracts.md`、`docs/architecture.md`、`docs/state.md`、Agent 指令及递归调用示例。本次沿用锁定的 Pydantic AI、PostgreSQL 和现有进程结构，不新增服务或依赖。
