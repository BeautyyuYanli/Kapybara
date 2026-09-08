Session 的回复与 loop 的结束分开处理。`reply_to` 每次完成选中输入并返回剩余回复地址；仍有未回复输入时，同一个 loop 继续运行。全部已读输入都已回复时，这次调用可以结束 loop。`text` 模式的正常文本结束在业务上等价于回复全部已读未回复输入。

这里的待回复集合只包含当前 session 已消费且尚未完成的直接输入，包括以前 run 留下的输入。尚未读入模型的 queue 不阻止本轮结束；waiting 结果没有新的回复地址。新 steer 仍在既有模型和工具边界接入，不能用此前的正文隐式回复它。

| 创建模式 | 模型完成回复的方式 | loop 的正常结束 |
| --- | --- | --- |
| `text`，默认 | 最终文本自动回复全部待回复输入 | 正常文本，或非空 `wait_for` |
| `reply_to` | 调用 `reply_to(ids)`，选择本段正文回复的输入 | 回复集合清空后的工具批次结束，或非空 `wait_for` |

`output_mode` 仍在创建时固定。普通模式不暴露 `reply_to`、being_waited_id 或相应 prompt，最终 output 保持框架返回的 str。显式模式中，纯文本仅提供回复正文，不单独结束 loop。`wait_for` 保持等待语义，允许在仍有回复义务时暂停；它不完成任何输入。

`reply_to` 的模型参数仍只有 `ids: list[UUID]`。运行时取得最近一条完整可见模型正文，构造完整 `ReplyTo`，再通过 State 提交回复。正文快照的来源规则保持不变：不取工具返回、thinking、流片段或新输入之前的旧正文。

State 保留现有 `ReplyTo` 和 `SessionOutput`，新增回复回执类型及一个 RunContext 方法：

```python
@dataclass(frozen=True, slots=True)
class ReplyResult:
    output: ReplyTo
    remaining_being_waited_ids: tuple[UUID, ...]

class RunContext(Protocol):
    async def reply(
        self, *, emission_id: UUID, output: ReplyTo,
    ) -> ReplyResult: ...
```

模型可见函数为：

```python
async def reply_to(ctx: AIRunContext, ids: list[UUID]) -> ReplyResult:
    output = runtime.reply(ids)  # 构造带正文的回复，目标资格由 State 校验。
    return await runtime.submit_reply(ctx, output)
```

`emission_id` 只由 Runner 提供，不进入模型 schema。`ReplyResult.output` 始终是完整 ReplyTo 对象，后续统一处理完整 output，不提取 payload 替代它。工具返回完整 ReplyResult，其中剩余地址按输入顺序排列；不增加可从列表推导的 done 字段。

例如模型已经读入 A、B、C：回复 A 后得到剩余 B、C，接着可以调用工具或组织下一段正文，再回复 B、C。第二次回执中的剩余列表为空，工具批次结束后直接结束 loop，无需再请求模型生成一条结束消息。本轮最终 output 是第二次的完整 ReplyTo；第一次回复作为独立输出保留在历史中。

同一调用最多选择 128 个不同地址。未知、外 session、尚未消费或已经结算的地址，均拒绝整次调用，不部分成功。`reply_to([])` 仅在当前没有待回复输入时允许，仍需有效正文；它能完成只由 waiting 结果触发、没有新增回复义务的一轮。成功回复属于正常工具返回，不通过 ModelRetry 伪装成参数错误；ModelRetry 只用于真实的参数或回复条件错误。

Runner 在显式模式下将 reply_to 注册为 `Tool(..., sequential=True)`，从 `output_type` 中移除它。`wait_for` 继续使用 ToolOutput，普通模式继续允许 str。普通工具会把返回值交回模型，而输出函数通常结束运行；这一区分由 Pydantic AI 原生处理。[函数工具](https://pydantic.dev/docs/ai/tools-toolsets/tools/)、[输出函数](https://pydantic.dev/docs/ai/core-concepts/output/#output-functions)

在现有 Boundaries 中增加 `after_node_run`，只在完整 CallToolsNode 批次结束后决定是否自动结束：

1. 框架已选中有效 WaitFor 时，保留这个等待出口；同批已经提交的回复依然有效。
2. 没有成功 reply_to、本批仍有工具重试、存在尚待接入的 steer，或最新待回复集合非空时，保留框架的下一节点，继续同一个 Agent run。
3. 最后一个成功 reply_to 已清空待回复集合，且本批工具和已接手输入都处理妥当时，将下一节点改为 `End(FinalResult(output=reply_result.output, tool_name="reply_to", tool_call_id=...))`。

Pydantic AI 的节点钩子支持返回下一节点或 End，并同步框架结果；因此条件结束仍产生正常的 AgentRunResult.output，不在外层手造运行结果，也不额外暴露 finish 工具。[节点钩子](https://pydantic.dev/docs/ai/api/pydantic-ai/capabilities/#pydantic_ai.capabilities.AbstractCapability.after_node_run)、[锁定版本的结果处理](https://github.com/pydantic/pydantic-ai/blob/v2.40.0/pydantic_ai_slim/pydantic_ai/run.py)

转换为 End 前，将框架刚生成但尚未加入历史的工具返回请求纳入完整历史并 checkpoint，保留一一对应的 tool call / tool return。节点钩子复用既有 steer 检查和 pending_final 恢复边界；检查之后才到达的新输入按既有事务边界启动后续工作。不得为了结束而丢弃同批已经请求的普通工具、工具返回或新输入。

多个 reply_to 调用在同一响应内依次执行，每次都根据前一次已提交的状态计算剩余集合。每个成功调用都具有实际回复效果；后续调用失败不会撤回前一个已成功的回复。一个调用选择多个地址时，仍保持这些地址的原子结算。部分回复不清除当前活动等待集合；只有最终文本、最终 ReplyTo 或显式 WaitFor 才按各自结束规则清理或替换集合。

State 的 `reply` 在一个既有 PostgreSQL 写事务中执行：

1. 检查 session 模式、run/attempt 有效性及操作幂等记录。
2. 读取当前 session 已消费未回复输入，校验整个目标集合及完整 output。
3. 计算回复后的 ReplyResult，并校验回执、历史记录及实际 waiting envelope 的编码大小。
4. 调用现有结算与投递逻辑：保存完整 output，完成选中请求的一次性通道，并向已等待的唯一接收者写入输入。
5. 同事务保存包含完整 ReplyResult 的 reply 记录，提交后调用现有 `_signal()`。

这次事务保持生产方 session/run 为 running，不创建新 run，也不清理其活动等待。接收方若已等待，可以立即继续；尚未等待时，结果保持 ready。reply_to 的 tool return 只在该事务成功后产生。

回复幂等复用现有 records 的 `(session_id, emission_id)` 唯一约束和 emission_fingerprint，不增加回复表或另一套队列。Runner 用已持久化的 ModelResponse 消息 ID 与 tool_call_id 派生 emission_id；记录同时限定 run_id，指纹包含完整 ReplyTo。相同操作重放返回原 ReplyResult，不重新投递、不以已经回复为理由拒绝；新调用再次选择已回复地址仍然是错误。原回执中的剩余集合是该操作提交时的快照，后续模型请求使用现有 `unreplied_addresses()` 获取当前集合。

模型响应、原始工具参数和已消费输入在调用 reply 前按既有 checkpoint 流程持久保存。若提交后、工具返回落盘前中断，恢复时重放相同 emission_id，取得原回执并补齐正常 tool return；若事务没有提交，则正常执行一次。目标是否仍未回复的检查放在 State 幂等查找之后，Runner 不在此前根据当前地址集合拒绝重放。恢复先完成旧工具批次，使用该调用原来的持久上下文构造正文，再接入新输入。工具恢复复用 reply_to 的完整函数参数 schema，继续拒绝额外 payload 参数及非法 JSON 形状。

回复成功后的失败、取消、租约丢失或 session 删除只处理仍未完成的输入，不能把已经回复的请求改写为 failed/deleted，也不能收回已经交接的结果。中途回复成功与整个 loop 最终成功是两个独立事实。

State 的 `_finish` 分支随之收敛：

- str：从同一份已消费未回复集合选择全部输入，复用回复结算逻辑，并在结束事务内完成。
- ReplyTo：确认它对应本 run 最后一条已提交回复记录、完整 output 一致且当前待回复集合已空，再保存最终 checkpoint 和结束记录；不再次发布已完成通道。
- WaitFor：保存最终 checkpoint、更新等待集合，不结算剩余输入。

RunResult 继续携带完整 SessionOutput 与最终 checkpoint，普通模式仍返回 str。

同一个 loop 可以产生多次回复，所以 cycle 的单值 output 改为按发生顺序保存完整 SessionOutput 的 outputs。每次成功 reply_to 都追加其完整 ReplyTo；最终 WaitFor 或普通文本也进入该序列。导致结束的最后一次 ReplyTo 已在序列中，不重复追加。框架的 AgentRunResult.output 仍只有一个，表示最终出口；中途回复是正常的结构化工具返回和持久回复记录。

压缩按统一 output 类型保留整个输出序列，不能只留下最后一次回复。reply_to 的结构化回复不随普通工具结果省略；第二级压缩保留输入和完整 outputs。未回复输入的上下文继续依据 State 的持久集合保护。前端继续展示文本流，final/waiting 保持既有 loop 边界语义；输入的完成状态仍通过已有请求查询接口读取。

显式模式的提示词改为：

> 每条直接输入的 being_waited_id 是它的回复地址。先写完整正文，再调用 reply_to(ids) 回复选中的输入，参数中不要填写正文。工具会返回本次回复和剩余的待回复地址。有剩余地址时继续处理，全部回复后本轮会自动结束。需要等待外部结果时使用非空 wait_for；等待不会回复输入。尚未读入的 queue 留给下一轮，waiting 结果没有新的回复义务。仅输出文本不会结束本轮。

普通模式沿用现有提示词。

实现涉及 State contracts、`RunContext.reply`、记录幂等及 `_finish`，Runner 的工具注册、完整批次结束钩子、恢复和多输出历史，以及 compression 和相关文档。复用现有 PostgreSQL 表、Valkey 提示和进程结构，不新增服务、依赖或公开 RPC。Runner 快照按新的输出序列结构提升版本，删除旧的“每次 reply_to 必然结束”恢复分支，不增加旧模式兼容转换。
