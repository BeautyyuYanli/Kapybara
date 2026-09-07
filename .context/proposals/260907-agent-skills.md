# Kapy v2：Agent Runner 与 Skills 方案

本方案依据 `kapy_v2.md`、`docs/architecture.md`、main 的 `docs/contracts.md`、`docs/acceptance.md`、`.context/delivery.md` 及总设计师最新统一裁决，范围为 `src/kapy/agent/`、`src/kapy/skills/` 及对应 tests。验收清单描述后续需要提供的证据，不代表这些检查已通过。只提交方案；总设计师审阅本修订稿并批准后才进入 cmd-impl。

**1. 实现边界与实际 API**

State 管理 session、运行串行性、输入领取、事件订阅、waiting 转换、历史和输出游标；Runner 只执行一次从唤醒到等待的推理过程。Gateway 创建服务、注入配置和 MachineCaller，Execution 执行进程及文件操作。不同 session 可以并发，不引入另一套 session/event 或 Pydantic AI 的持久化后端。

已执行 `uv sync --locked`，实际安装 Python 3.14.4、pydantic-ai-slim 2.40.0、openai 3.8.0、httpx2 2.12.0。以下结论来自当前 `.venv/lib/python3.14/site-packages/pydantic_ai/` 源码及仅使用 dummy key 的本地 mock：

- `Agent` 使用 `capabilities`；历史处理使用 `ProcessHistory` / `AbstractCapability.before_model_request`，不能照搬旧版 `history_processors=`。
- `Agent.run_stream_events(...)` 返回异步上下文管理器；`RunContext.enqueue(..., priority="asap")` 可在工具/模型边界注入 steer。框架会在自然结束前处理已注入的输入。
- `AgentRunResult.usage` 是 `RunUsage` 属性，使用 `result.usage.input_tokens` 等字段，不调用 `result.usage()`。总设计师报告锁定版本与 gpt-5.6-luna 已通过一次无副作用工具、两次模型请求的 live 基线；此处与本 senior 的本地 mock 证据分开记录，不重复读取主目录配置或调用模型。
- `Tool.from_schema(function, name, description, json_schema, takes_ctx=False, sequential=False, args_validator=None)` 跳过 schema 校验；插件必须用自己的 Pydantic 参数模型验证。
- `ToolReturn.return_value` 可直接含 `BinaryContent`。Chat 模型会将其映射成配对的文字 tool message 和附加的媒体 user message；不用 `ToolReturn.content` 人工拆出另一条媒体输入。
- `ProcessHistory` 的结果会替换当前运行的消息历史，不能把最终 `new_messages()` 当作未压缩的历史归档。
- Chat 模型没有实现服务端 `count_tokens`；本地 profile 对此模型也未给出 context window。`ModelMessagesTypeAdapter` 是实际可用的消息序列化边界。
- 流式 mock 中，普通 `wrap_model_request` 没接住延迟启动时的媒体 400；在 Runner 外层捕获、替换最近请求的媒体返回并续跑成功。共三次模型请求，`read_media` 只执行一次；重试保留同一 `tool_call_id`。

模型构造使用实际安装的 `OpenAIChatModel("gpt-5.6-luna", provider=OpenAIProvider(base_url=..., api_key=..., http_client=...))`。由 Gateway 显式传入 base/key，不在 agent 模块自动读取 `.env`。首版采用 Chat Completions，完整上下文由本地传入。OpenAI 官方给出的该模型窗口为 1,050,000；自定义 endpoint 的实际限制可能不同，因此窗口必须可以覆盖配置。[模型资料](https://developers.openai.com/api/docs/models/gpt-5.6-luna)

**2. Runner 导出与 State 契约**

`Runner`、`RunnerConfig` 从 `kapy.agent` 导出，初始化入口采用 main docs/contracts.md 明确批准的 Runner.initial_state。Runner 直接满足 State 已发布的 `SessionRunner = Callable[[RunContext], Awaitable[RunResult]]`；所有运行 DTO 从 kapy.state 导入，不在 agent 包定义或重导出另一套类型。MachineCaller 使用 kapy.rpc 的共享协议，由 Gateway 实现。State 的 Python ID 使用 UUID，转换到 RPC/Runner JSON 时使用字符串。

```python
from kapy.rpc import MachineCaller
from kapy.skills import SkillDescription
from kapy.state import (
    CheckpointWrite, Cursor, JsonObject, JsonValue, MessageWrite,
    OutputDelta, RecordPage, RunContext, RunResult, RunnerState,
    SessionInput, SessionRunner, SessionView,
)

type AuthorizeWait = Callable[[UUID, tuple[UUID, ...]], Awaitable[None]]

@dataclass(frozen=True)
class RunnerConfig:
    base_url: str
    api_key: SecretStr
    context_window_tokens: int
    model: str = "gpt-5.6-luna"
    max_output_tokens: int = 16_384
    compression_ratio: float = 0.70
    keep_recent_ratio: float = 0.10
    media_max_bytes: int = 20 * 1024 * 1024

class Runner:
    def __init__(
        self, config: RunnerConfig, machine_caller: MachineCaller, *,
        http_client: httpx2.AsyncClient,
        payload_store: AgentPayloadStore,
        authorize_wait: AuthorizeWait,
        plugins: Sequence[ScriptTool] = (),
    ) -> None: ...

    def initial_state(
        self, *, instructions: str, skills: Sequence[SkillDescription],
    ) -> RunnerState: ...

    async def __call__(self, context: RunContext) -> RunResult: ...
```

`Runner(...)` 构造器即 factory，不额外增加 create_runner。构造不启动 I/O、后台任务或 session；无 start/aclose 生命周期。Runner 必含内置工具与 apply_patch；plugins 只添加自定义工具，重名立即报错。Gateway 持有并关闭共享 HTTP client；Runner 借用它，绝不关闭注入资源。Pydantic Agent、capabilities 和每次运行的可变数据在 `__call__` 内创建，避免多个 session 共享可变消息、媒体修复标记或等待结果。Gateway 将该实例直接作为 SessionRunner 注入 State，State 负责串行调用、取消和恢复。

Gateway 创建 session 时先 `await skills.catalog()`，再调用 `runner.initial_state(instructions=用户配置, skills=完整目录)`，把结果交给 SessionSpec.initial_state。该同步方法使用构造 Runner 时注入的 config，是唯一上下文初始化入口，不增加另一函数或兼容别名。它将基础 prompt、用户 instruction 和全量 descriptions 固定到 `RunnerState(codec="kapy.agent.v1", data=...)`，检查 config 与实际 JSON 大小满足持久化限制；config 中的 API key/base 等连接配置不写入 RunnerState。该方法不查数据库、不调用模型，也不估算 token。首次模型输入 token 数尚未知。Runner 不持有 SkillService，也不每轮刷新 catalog。恢复使用 State 已保存的 instruction；输入和上下文分别来自 context.inputs、context.state。

RunnerConfig 提供服务默认值；每次运行从 context.session.config 的可选 model 字段选用模型名称，缺失时使用 RunnerConfig.model，非法类型/空串明确拒绝。Gateway/State 仅允许在 waiting 且无活动 run 时更新 config；本轮使用启动快照，不在模型重试中热切换模型。用户 instructions 与 skill catalog 是创建时快照，后续 /instructions 只更新 Gateway 的 saved config，供下一次 /new 使用，不改写既有 session instruction 或 codec。机器存在性与关联鉴权统一由 Gateway 的 MachineCaller 处理，Runner 将未关联/不存在错误反馈为可纠正的 tool response。没有默认机器时，工具省略 machine_id 会得到可纠正的参数错误。

State 已发布提交 4b423490884ef824c0dae652cb82073a0f7759bb 的 kapy.state.contracts 及 __init__ 是运行类型的源契约；以下名称已真实导出，直接导入使用。SessionService/migrate 由 State 完成实现后导出，agent 不创建兼容类型或临时替代。以下仅列使用方式与字段，不复制类定义：

| State 导出 | Runner 使用的既定形状 |
| --- | --- |
| RunnerState | codec:str、data:JsonObject |
| MessageWrite | message_id:UUID、kind:model_request/model_response、text:str、data:JsonObject |
| CheckpointWrite | number:int、state:RunnerState、messages:tuple[MessageWrite,...]、consumed_input_ids:tuple[UUID,...] |
| OutputDelta | emission_id:UUID、message_id:UUID、kind:text_delta/tool_call/tool_result/notice、data:JsonValue |
| RunResult | output:str、wait_for:tuple[UUID,...]、checkpoint:CheckpointWrite |
| RunContext | session:SessionView、run_id:UUID、attempt:int、recovered:bool、inputs:tuple[SessionInput,...]、state:RunnerState、checkpoint_number:int |

RunContext 的既有方法为 `poll_steer(*, limit=64) -> tuple[SessionInput,...]`、`emit(delta:OutputDelta) -> Cursor`、`checkpoint(write:CheckpointWrite) -> Cursor`、`read_history(*, after:Cursor|None=None, limit=200) -> RecordPage`，均为 async。Runner 不再定义 emit envelope；下文仅规定 OutputDelta.data 的 agent 私有内容。SessionInput 保留 id、seq、mode、payload、event_id；payload 是 JsonValue，字符串直接作为文本，其他 JSON 保留完整结构编码后交模型。

`context.inputs` 是本轮已 reserved 的至多 64 条输入，按 seq 处理；`context.poll_steer(limit=64)` 领取新 steer，同一运行不重复返回。reserved 不等于 consumed；只有包含该输入的 checkpoint 成功才确认消费。Runner 在模型节点前、工具节点前后、准备结束前领取并 enqueue；queue 由 State 留到下次唤醒。大批输入按预算逐条注入和 checkpoint，不无条件拼成一个超大 ModelRequest。结束前必须处理完自己已经领取的全部输入；框架自然结束也不能跳过尚未注入的 reserved 输入。最后一次轮询后才到的输入保留给下一轮，不由本轮提前完成。

`context.checkpoint(CheckpointWrite(...))` 原子追加原始完整消息、保存上下文投影、确认输入消费。Runner 本地从 context.checkpoint_number 连续加一；同 run/number/内容重试返回原 Cursor，不同内容冲突，State 校验当前 attempt 并拒绝迟到写入。模型响应完整落定后、工具执行前、结果落定后、压缩前提交；流式碎片不能当作可执行的完整 tool call。每条新增完整 ModelMessage 只归档一次，checkpoint 重试使用相同 message_id/number/content，不依赖 State 重新推断消息增量。

`MessageWrite.data` 是一条新增完整 ModelMessage 的 JSON，空增量使用 `messages=()`。编码器先将本模块 read_media 的 BinaryContent 保存为持久 payload，再把它在 ToolReturnPart.content 列表中的位置替换成文字占位；ToolReturnPart.metadata 的 kapy_media_refs 保存该位置、media_type 与 PayloadRef。剩余结构使用 ModelMessagesTypeAdapter 编码，仍是合法的 Pydantic 消息 JSON。恢复时由 agent codec 解析 metadata 并重新构造 BinaryContent；State 不解释内容。原始历史不因压缩或媒体替换而覆盖；修复投影删除媒体引用并换成错误文字，原始媒体继续保留。

State 限额分别是单条完整消息 256 KiB JSON、delta 16 KiB、checkpoint 4 MiB；对外 output/history/query 页面则是包含分页字段的实际 JSON 编码总量最多 512 KiB，不能混用这几个限额。Runner 在序列化后检查实际字节数；工具展示限额在此之前控制，模型超大完整响应明确报资源错误，不截断 tool arguments 后执行。媒体移出后仍超限的单条消息不通过偷偷放宽 State 限额解决。这些是存储/RPC 大小限制，绝不用于换算 token。

媒体及超过 checkpoint 可内联预算的上下文使用下面的持久 payload 接口。它仅保存不可变 bytes，不增加 session、event、history 顺序或运行状态表；所有权与删除编排遵循总设计师已裁决的 Intelligence store/Gateway 借 pool 方案。

```python
@dataclass(frozen=True, slots=True)
class PayloadRef:
    sha256: str
    bytes: int

class AgentPayloadStore:
    def __init__(
        self, pool: AsyncConnectionPool, *, schema: str = "kapy_agent",
    ) -> None: ...
    async def initialize(self) -> None: ...
    async def put(self, session_id: UUID, data: bytes) -> PayloadRef: ...
    async def get(self, session_id: UUID, ref: PayloadRef) -> bytes: ...
    async def delete_session(self, session_id: UUID) -> None: ...
```

PayloadRef、AgentPayloadStore 从 kapy.agent 导出。store 借用 Gateway 自有 metadata pool；构造无 I/O，initialize 只创建其受信 schema 中的 agent_payloads，无后台任务和 start/aclose。表字段为 session_id UUID、sha256 TEXT、bytes BIGINT、data BYTEA，主键 (session_id,sha256)，不跨包引用 State 物理表。put 在单个短事务中按同 session/hash 幂等保存 bytes，最多 64 MiB；get 总以 session_id 与 hash 查询并核对长度/摘要，缺失或损坏抛 PayloadNotFound/PayloadCorrupt，超限抛 PayloadTooLarge，三个异常从 kapy.agent 导出。不能回机器重新读取可能变化的文件来掩盖恢复错误。

Gateway 在删除前持久化其清理义务；State 停止并等待该 session 的 Runner、完成删除后，Gateway 的 durable cleanup 调用幂等 delete_session，删除该 session 的全部 payload。恢复未完成清理继续使用同 session_id。整个控制服务正常停机不调用 delete_session；payload 必须保留用于重启。store 不拥有清理 outbox、订阅或 session 生命周期；Runner 取消时必须等待自己尚在执行的 store 操作收束，避免删除后迟到写入。初始化创建 snapshot 时尚无 session，只保存内联初始数据，不写 store。

带媒体引用的 RunnerState.data JSON 超过 2 MiB 时，整体编码为 UTF-8 bytes，先 put 成功，再在 State checkpoint 中保存 `data={"version":1,"payload":{"sha256":"...","bytes":123}}`；载入后还原下方同一 v1 结构，不改变 codec。小上下文保持内联。每次 checkpoint 只附本边界新增的一至两条完整消息，连同小引用/≤2 MiB 内联状态满足 4 MiB 总限额。最大 payload 64 MiB 是显式恢复预算，超限报告资源错误。put 早于 State checkpoint，崩溃最多留下未引用 payload，不会提交悬空引用；首版随 session 删除统一回收，不增加引用计数或后台 GC。媒体原件和外置投影都只存本 PostgreSQL，控制进程重启不依赖本地临时目录、Execution 当前文件或 URL 有效期。

`RunnerState.data` 解引用后为 Runner 拥有的 JSON 文档，State 不解释内容。v1 形状如下，messages 为上述 codec 的消息数组，媒体使用持久引用；检查点序号由 State 管理：

```json
{
  "version": 1,
  "instructions": "基础 prompt、用户 instruction 和创建时目录",
  "skill_descriptions": [{"id": "skill-uuid", "description": "技能描述"}],
  "last_usage": null,
  "media_fallback_call_ids": ["read-media-call-1"],
  "pending_tools": [],
  "cycles": [
    {"turn_id": "t1", "closed": true, "level": 1, "messages": []},
    {"turn_id": "t2", "closed": false, "level": 0, "messages": []}
  ]
}
```

cycle 表示两次 waiting 之间的上下文段；只有最后一段允许未关闭。cycle 中空 messages 仅用于展示形状，实际投影必须保留当前请求。media_fallback_call_ids 只保留投影中仍存在的已修复调用，随对应区间丢弃，防止恢复时重试预算重置。turn_id 使用 State run_id 的字符串，在恢复同一轮时不变；Pydantic 的 run_id 可因媒体重试而更新。派发前将工具意图、确定后的 machine_id、Execution process_id/transfer_id 及参数写入 checkpoint；这些 UUID 由 session_id/run_id/tool_call_id/子步骤稳定派生，不增加 operation_id 字段，也不依赖会变化的 attempt。未决操作记录在 RunnerState.data.pending_tools 中，包含恢复所需 argv、传输摘要、已确认 offset 和各路输出 cursor，不包含凭证。

恢复时先查已知 process_id/transfer_id，不换 ID 重发命令。若无法确认曾否执行或结果已不可恢复，为原 tool_call_id 写入 `outcome_unknown` 文字结果并继续 loop；process.write 没有可重放 ID，不做自动重发。取消/控制进程中断使用 State 同 run_id、attempt+1 和最新 checkpoint 恢复；普通模型/工具不可恢复异常交 State 以 failed 完成本轮，不能把两种情形混成无界自动重试。已完成工具结果必须在下一模型请求前 checkpoint。

`context.emit(OutputDelta(...))` 接收 State 的 `emission_id: UUID`、`message_id: UUID`、kind 和 data，返回持久化 Cursor。data 形状如下，attempt_id 是模型请求尝试 ID，不等于 State run_id/attempt：

```text
kind="text_delta": {"attempt_id":str,"part_index":int,"text":str}
kind="tool_call": {"attempt_id":str,"tool_call_id":str,"name":str,"args":JsonObject}
kind="tool_result": {"attempt_id":str,"tool_call_id":str,"result":JsonValue}
kind="notice": {"kind":"attempt_failed","attempt_id":str,"failed_message_id":str,"code":str,"message":str}
```

工具结果中的大文件/媒体使用引用、字节数、类型等摘要，不广播 base64。每次模型响应尝试使用新的 message_id，失败 notice 标明 failed_message_id；最终完整消息沿用成功尝试的 message_id，前端据此替换 delta 投影。State 单条 delta 上限 16 KiB，text_delta 按编码后大小分块；大 args/result 在 delta 中仅给出明确摘要及完整消息关联，不截断后伪装成完整参数。

Runner 返回 RunResult 的最终 checkpoint 必须使用下一序号，且不先调用 context.checkpoint 提交这一份。State 原子保存最终 checkpoint/output、进入 waiting、替换外部订阅并保留 own channel、处理 backlog、向已消费请求发 completion。即使已有 queue/backlog，也必须先经过这个 waiting 边界。Runner 不发 completion、不自行等待 channel；普通异常与取消原样交 State。`AuthorizeWait` 从 kapy.agent 导出，由 Gateway 提供基于调用 session 的频道授权检查；返回 None 表示通过，拒绝抛 PermissionError 并提供不含秘密的说明。它不订阅频道，也不改变 State 生命周期。

**3. 内置工具与 Execution RPC**

以总设计师 docs/contracts.md 和 Execution 已冻结协议为准。Runner 使用 `MachineCaller.call(machine_id: str, method: str, params: JsonObject, *, timeout: float = 60.0) -> JsonValue`；timeout 包含 Gateway 等待上线与本次调用，不等于 process wait_ms，也不意味着取消远端副作用。模型选择 machine_id，Runner 从 context 注入 session_id；凭证仅由 Gateway 在关联/重连时交给 session.ensure。模型不能填写 session_id、session_token、process_start 的 process_id 或 transfer_id。

模型侧工具如下，参数描述从 agent 使用角度撰写；ProcessCursor 与下文 wire Cursor 同形，类型归 agent 内部，不能与 State 的不透明 Cursor 混用。全部机器工具默认 sequential。

```python
async def process_start(
    command: str, *, mode: Literal["stdio", "pty"] = "pty",
    machine_id: str | None = None, cwd: str | None = None,
    wait_ms: int = 1000,
) -> JsonObject: ...

async def process_wait(
    process_id: str, *, machine_id: str | None = None,
    cursor: ProcessCursor | None = None, wait_ms: int = 1000,
    max_bytes: int = 65_536,
) -> JsonObject: ...

async def process_write(
    process_id: str, input: str, *, machine_id: str | None = None,
) -> JsonObject: ...

async def process_resize(
    process_id: str, rows: int, cols: int, *,
    machine_id: str | None = None,
) -> JsonObject: ...

async def process_kill(
    process_id: str, *, machine_id: str | None = None,
    wait_ms: int = 5000,
) -> JsonObject: ...

async def process_list(
    *, machine_id: str | None = None, after: str | None = None,
    limit: int = 50,
) -> JsonObject: ...

async def process_release(
    process_id: str, *, machine_id: str | None = None,
) -> JsonObject: ...

async def file_read(
    path: str, *, machine_id: str | None = None,
    offset: int = 0, limit: int = 65_536,
) -> JsonObject: ...

async def file_write(
    path: str, content: str, *, machine_id: str | None = None,
) -> JsonObject: ...

async def read_media(
    path: str, *, machine_id: str | None = None,
) -> ToolReturn: ...

async def wait(wait_for: list[str]) -> WaitRequest: ...
```

process_start 将 command 显式映射为 `["/bin/sh", "-lc", command]`，使用选定 mode；插件另走纯 argv。process_write 将 input 编码为 UTF-8/base64，仅写 PTY，返回 accepted_bytes；随后模型用 process_wait 观察。输入中的 Ctrl-C 只是终端字节，raw mode 不保证发送信号；process_kill 请求 Execution 清理进程组及可追踪后代，采用用户指定的 best effort，不保证回收所有逃逸进程。process_release 明确删除已结束进程的完整输出，不自动调用。file_read/file_write 是模型侧适配器名称，不是 wire 方法。

wait 使用 `ToolOutput(wait, name="wait")` 与文字输出并存，唯一参数是 wait_for；最多 128 个合法 UUID，按首次出现去重。私有 WaitRequest 记录去重结果；返回之前调用注入的 authorize_wait，拒绝时作为可纠正工具参数错误继续 loop。采用 `end_strategy="exhaustive"` 与 sequential，同批完整 tool calls 都落定后结束。自然文字结束使用空 wait_for；显式 wait 前的文字作为 output，没有文字则空串。State 始终保留 own input channel，外部订阅 sticky 到下一次成功 RunResult 时替换；Runner 不做 channel 订阅或阻塞等待。

以下全部 RPC params 包含 `session_id: str`，表格只列其余字段。process_id/transfer_id 为调用方 UUID 的字符串；未列字段禁止发送，可选值缺失时省略，不发送任意 null。所有结果放在 JSON-RPC 2.0 result 中。

```text
Cursor = {pty: int} | {stdout: int, stderr: int}
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
          | {kind: "url", url: str, headers?: dict[str, str] = {}}
```

| 方法 | 除 session_id 外的 params | result |
| --- | --- | --- |
| `session.ensure` | `{session_token:str}` | `{session_id:str,cwd:str}` |
| `session.release` | `{wait_ms?:int=5000}` | `{session_id:str,released:bool}` |
| `process.start` | `{process_id:str,mode:"stdio"\|"pty",argv:list[str],cwd?:str,env?:dict[str,str]={},rows?:int=24,cols?:int=80,wait_ms?:int=1000}` | ProcessUpdate |
| `process.wait` | `{process_id:str,cursor?:Cursor,wait_ms?:int=1000,max_bytes?:int=65536}` | ProcessUpdate |
| `process.write` | `{process_id:str,data_base64:str}` | `{accepted_bytes:int}` |
| `process.resize` | `{process_id:str,rows:int,cols:int}` | ProcessInfo |
| `process.kill` | `{process_id:str,wait_ms?:int=5000}` | ProcessInfo |
| `process.list` | `{after?:str,limit?:int=50}` | `{items:list[ProcessInfo],next:str\|null}` |
| `process.release` | `{process_id:str}` | `{released:true}` |
| `file.push` | `{transfer_id:str,path:str,size:int,transport:Transport,sha256?:str}` | TransferInfo |
| `file.pull` | `{transfer_id:str,path:str,transport:Transport}` | TransferInfo |
| `file.chunk` | `{transfer_id:str,offset:int,data_base64?:str,max_bytes?:int=65536}` | push: `{next:int}`；pull: ByteChunk |
| `file.finish` | `{transfer_id:str,wait_ms?:int=1000}` | TransferInfo |
| `file.abort` | `{transfer_id:str}` | `{aborted:bool}` |

session.ensure/release 是 Gateway 生命周期操作；Runner 不创建/删除机器 session。Runner 从 process.start 返回的 cwd 或正常 `pwd` 命令获取绝对路径，平台架构用正常 `uname -m` 命令查询，不扩展 ensure result。

Cursor 默认与 mode 匹配的各路 0，单位是原始 bytes。wait_ms 范围 0–30,000，0 表示立即快照；max_bytes 1–65,536，stdio 每路独立受限，PTY 再受 8192-byte 尾窗限制。start 返回后仍可 running；timeout 不杀进程。exit_code、output_complete 由 Execution 报告，Runner 不从一段安静输出推断终止或清理成功；PTY 正常窗口淘汰仍可能 truncated。清理边界以最新 best effort 契约为准，不沿用旧 cgroup 保证。

Runner 为每个 stream 独立维护增量 UTF-8 解码器与 next cursor，跨块保留未完成字符；模型跳转 cursor 或 PTY truncated 时重置解码状态，并明确显示丢失的前缀。解码后的展示每次有界；Wire 原始 bytes 保存在 Execution spool，不要求 Execution 返回文件路径。工具/State 中的完整输出引用为 `{machine_id,session_id,process_id,stream,cursor}`，通过 process.wait 分块重读，只有显式 release 后失效。不能把这一可释放引用冒称控制面的永久历史副本。

process.start 对相同 UUID/启动参数只观察，不重跑；wait_ms 不参与启动指纹。恢复先对已持久化 process_id 调 process.wait。process.write 仅支持 PTY，decoded≤64 KiB；断线和部分写失败不能自动重发，已知 accepted_bytes 必须展示。process.resize 尺寸 1–1000；process.list limit 1–100；process.kill 幂等，未完成返回 killing；process.release 对运行中进程报 conflict、终止后保留 tombstone。

文件传输优先复用 WebSocket。file.push 不创建父目录，按声明 size 顺序写 staging，finish 在大小/hash 校验后原子替换。push chunk 必有 data_base64，不传 max_bytes；只接受当前 offset 或最后一块完全相同的重放。file.pull 固定普通文件 fd；chunk 可按 offset 读取，available 是打开时 size，TransferInfo.offset 只是发出位置的高水位，不能视为调用方已接收。pull 不计算 SHA-256，sha256 为 null；接收方自行累计 hash。传输期间与 finish 检查 inode/size/mtime/ctime，变化报 file_changed；这不是不可变文件快照。

file_read 以 pull/chunk 读取请求区间，finish 验证成功后才展示，返回 `{path,offset,next_offset,eof,size,text}`；UTF-8 非法内容返回二进制提示。file_write 将有界 UTF-8 内容（最多 64 KiB）以 push/chunk/finish 写入，返回 `{path,bytes,sha256}`，缺失父目录明确报错。大文件由 CLI 或脚本操作，不扩大 model tool 参数。read_media 与 skill upload 必须收齐完整 bytes、核对累计 size 并成功 finish 后才使用或发布；失败/取消 abort 自有传输。

传输 UUID 与参数绑定；控制连接重连可查询同 ID 并继续已确认 offset，daemon 重启把活动传输标 failed，不承诺跨 daemon 续传。终态 begin 不重新开始。URL push 是机器 HTTP GET，URL pull 是机器 HTTP PUT；Gateway 提供 URL/headers，Runner 不保存认证参数、不增加对象存储客户端。URL PUT 结果未知不得自动重试。单条 RPC message 上限 1 MiB，decoded 64 KiB chunk 的 base64 消息约 88 KiB，完整文件永不塞入一个 RPC。

**4. 脚本插件与 apply_patch**

不增加远程插件 registry、动态 Python 加载或第二套进程管理器。插件是启动时显式注入的 Python 定义，参数和 description 可定制，执行结果统一走 process.start(mode="stdio") 和 process.wait。

```python
@dataclass(frozen=True)
class ProcessCommand:
    argv: tuple[str, ...]
    stdin: bytes | None = None
    cwd: str | None = None

@dataclass(frozen=True)
class ScriptTool[P: BaseModel]:
    name: str
    description: str
    parameters: type[P]
    render: Callable[[P], ProcessCommand]
```

插件参数模型提供业务字段，注册器统一加保留字段 machine_id，不允许业务模型覆盖它。wrapper 去掉 machine_id 后以 `parameters.model_validate` 校验原始参数，再调用 render。JSON Schema 来自该模型的 `model_json_schema()`，Tool.from_schema 只负责向模型公布 schema。render 可返回解释器 argv 和脚本 stdin；用户字段不拼接进 shell 模板。工具 description 讲任务、参数与行为，不向模型介绍 RPC、数据库、Python 或压缩实现。

ProcessCommand.stdin 是插件适配器的进程内字段，不扩展 Execution RPC。Execution stdio stdin 为 /dev/null；有 stdin 时先以 file.push/chunk/finish 写入 session 内稳定命名的临时文件，再执行 `argv=["/bin/sh", "-c", "exec \"$@\" < \"$0\"", absolute_stdin_path, *command.argv]`。程序固定，路径放在 $0，程序与参数放在 "$@"，业务内容不参与 shell 解析。准备目录用独立 `mkdir -p -- path` argv；记录每个准备/执行步骤的稳定 UUID。输入文件在进程终止并确认后清理，运行中或 outcome_unknown 时保留，最终随机器 session 清理；取消不误删仍在使用的文件。

第一个插件导出模型工具 `apply_patch(patch: str, machine_id: str | None = None)`，通过固定 argv 指向目标机器的 apply_patch 可执行文件，patch 原文经上述文件重定向走 stdin；保持 LF，不把 patch 放进 shell heredoc。非零退出码按普通工具结果反馈，不假定多文件 patch 失败时已回滚。

采用已核对的 [codex-apply-patch 发布包 rust-v0.153.4](https://github.com/BeautyyuYanli/codex-apply-patch/releases/tag/rust-v0.153.4)。Linux x86_64 tar.gz 的 SHA-256 为 `d2b6db33f1ebdbca9691237287fe25ee4fe53b90f0255a7e3cfb82b70c721567`；aarch64 为 `0b518abbbd75016f615a6164c45f1a7571c3be6d6d55559f3019ee4913cfe0b5`。包含 SKILL.md、binary、LICENSE、NOTICE、SOURCE 和第三方许可。

工具 description 以该发布包完整 [SKILL.md](https://github.com/BeautyyuYanli/codex-apply-patch/blob/8639ac2d93442bcec5631b693b4ed7c0144422b7/SKILL.md) 为来源。它的原始传输约定是 FREEFORM，与 Pydantic 普通 function tool 的 JSON 参数不一致：生成 description 时只改写传输约定，说明外层是参数对象、patch 字段内容是原始补丁；保留其语法、示例和行为说明。catalog 的 description 则使用 frontmatter 的简短 description，二者用途不同。该 revision 的上游 CLI tests 明确覆盖无 argv 补丁参数时通过 stdin 输入。

批准后在本模块提供同步生成器：从固定 release 获取并校验 bundle，生成随包的 SKILL 来源与 manifest；生成文件只通过该工具更新。运行时以正常 process.start 查询 uname -m，按 manifest 获取对应 Linux binary；在 session cwd 下建立带内容 hash 的受管目录，经 file.push/chunk/finish 安装 binary 与许可文件，完成后用普通 `chmod 700 -- binary_path` argv 赋执行位。file.push 不接受 mode，不隐含创建目录。同 session/hash 的已完成安装复用；缺失平台或下载失败返回明确工具错误，不换成自制 patch。SKILL.md 的路径约束是给模型的说明，不将它误称为 binary 提供的文件系统沙箱。

**5. 媒体拒绝后的恢复**

read_media 发起 file.pull，先检查 size≤20 MiB，再逐块 file.chunk 收齐全部 bytes、检查连续 offset/累计 size，并计算本地 SHA-256；file.finish 确认 complete 且未发生 file_changed 后才构造媒体。pull.sha256 为 null，不能虚构远端摘要或 file version。变化/短读/超限/无法读取时 abort 并返回文字错误，不把混合内容发给模型。可识别的媒体构造 `ToolReturn(return_value=[文字说明, BinaryContent(...)], metadata=来源信息)`，来源包含 machine_id、path、media_type、SHA-256 和 tool_call_id。不能识别的类型直接返回文字，不把文件名后缀当作模型能力保证。

每个模型请求前保存可恢复的完整投影，其中已包含此前所有 tool call/return。模型拒绝媒体时，Runner 外层对该请求的 read_media 返回进行以下处理：

1. 根据工具归属和 media part 定位受影响的 tool_call_id；删除这些返回中的媒体，改为包含 provider 错误代码及文字的 tool response。多个媒体无法精确归因时替换该请求中全部 read_media 媒体，并明确说明此次请求被拒绝，不能声称每个文件都不受支持。
2. 保留 tool name、tool_call_id、同批其他结果和输入次序；持久化修复后的投影及 attempt_failed，再用 `user_prompt=None` 和该请求历史创建新的 Pydantic run。不会再次调用进程、读取媒体或消费同一输入。
3. 对同一批媒体只做一次文字重试。后续模型主动调用新的 read_media 是新的工具调用；已拒绝的旧媒体不能在下一轮从原始历史重新注入。

HTTP 400/422 必须带媒体字段/类型/解码相关错误证据才归入此分支。模型适配器对已知媒体的序列化不支持也转换为文字。鉴权、限流、普通网络错误、上下文过大不假称媒体拒绝；重试后的错误交 State 处理。provider 错误文字限 8 KiB，去除认证信息、URL query 和大块 base64，保留诊断内容。中途已流出的失败尝试通过 attempt_id 标记，不执行不完整的 tool arguments。

**6. 分层 context compression**

压缩仅改变 Runner 的模型上下文投影。State 原始 PostgreSQL 历史完整保留，基础 instruction 说明可用 `kapy control history` 查找旧记录；不引入向量库、LLM 总结、摘要树或独立检索系统。[Pydantic 消息历史说明](https://pydantic.dev/docs/ai/core-concepts/message-history/)

窗口 C 来自配置。每次完整模型响应保存该响应自身的 `ModelResponse.usage`（RequestUsage），按用户最新最高优先级规则使用 `input_tokens >= 0.70 * C` 决定在下一次请求前进行一次压缩。input_tokens 是该次完整请求的 API 实报值，按 provider 定义包含 cached input，不减去 cache_read_tokens，也不重复加上它；output_tokens 可保留用于记录，但不加入这一压缩判据。不能用整个 AgentRunResult.usage 的累加值判断单次上下文；result.usage 仍是可用于报告的属性。实际 Chat 适配器为流式请求设置 include_usage；未收到有效 usage 或适配器仅有缺省全零值时，计数视为未知。

不调用 tiktoken，不按字节、字符或媒体大小换算 tokens，不给媒体添加估算 reserve。第一次请求没有历史 usage，不能在创建 session 时虚构 token 计数；initial_state 只校验配置和持久化大小。max_output_tokens 作为发送给模型的输出上限配置，不声称仅凭前一次请求的 usage 能精确预测下一次请求大小。

RunnerState 保存 `last_usage={response_id,model,input_tokens,output_tokens,cache_read_tokens,sweep_applied}`；response_id 是 Runner 已持久化的模型 message_id，不依赖 provider 一定返回 ID。没有有效观测时 last_usage 为 null；session 切换模型后也先视为未知，不把另一模型的 usage 当作新模型的计数。响应落定后在 checkpoint 保存 usage；下一次模型请求前达到阈值且 sweep_applied=false 才执行一轮，压缩结果与 sweep_applied=true 一起 checkpoint。恢复不因同一旧 usage 再降一级；等待下一次实际响应的全新 usage 后才重新判断。

最新约 10% 按完整交互块数量近似：保护最新 ceil(总块数×keep_recent_ratio) 块，至少一块；这不是 token 百分比。再向前扩展以保护完整 user/model/tool 配对、当前输入和尚未配对完成的工具链。固定 instruction 和创建时 catalog 不降级。closed cycle 跨过保护边界时整段保护，允许超过 10%；当前未关闭 cycle 只有旧的完整交互块可降到 level 1。

| 级别 | 模型看见的内容 |
| --- | --- |
| 0 | 原始消息及结果；已拒绝媒体使用文字修复投影；当前 cycle 的局部 level 1 例外见下文 |
| 1 | 保留输入、assistant 文字及工具名称/参数；每个工具结果只保留同一 ID 的明确省略占位，不保留原 response 内容或媒体 |
| 2 | 只保留这一 waiting 区间的全部输入（含 steer/event 输入）与最终 output，重建普通 user/assistant 消息 |
| 丢弃 | 从投影删除整个区间，原始历史仍可查询 |

一次 sweep 按开始时的 level 同步推进旧区间：0→1、1→2、2→丢弃，每区间只降一级。最新保护集合在本次 sweep 中固定。压缩后不拿旧 usage 连续执行第二、第三轮，不自行计算“压缩后 tokens”。level 1 用占位 tool return 满足 OpenAI 配对约束，不能直接删除 return 留悬空 call。

压缩的最小单位包含 assistant 中的整批 tool calls、对应全部 tool returns 和 retry parts；不截断 JSON 参数，不产生悬空/重复 tool_call_id。level 2 或丢弃时删除整组工具协议消息；最后仍必须有当前 ModelRequest。原始消息在压缩前已 checkpoint，压缩 level、usage 观测标识与投影同步持久化，所以重启不会将旧消息升回 0。level 1 同时移除该工具返回的 kapy_media_refs，避免恢复时把已省略媒体重新填回去。

当前尚未结束的 cycle 中，已完成且不属于最新保护区的交互块参与 0→1；占位结果用 ToolReturnPart.metadata 的 kapy_compression:1 记录，cycle.level 暂仍为 0，直到整段完成 0→1。当前 cycle 不做 1→2 或丢弃，不能提前捏造等待输出。

provider 明确返回 context-too-long 时，将这次错误作为新的失败观测，对同一请求投影执行一次既定 sweep 并有限续跑，不执行工具重放；每条新错误响应最多一轮，整次连续失败最多两次压缩重试。重试次数与修复投影先 checkpoint，恢复不重置上限。若没有可降级区间或重试仍失败，抛 ContextBudgetExceeded 给 State；不能裁剪固定 instruction/受保护输入，不能把普通网络或鉴权错误当作上下文超限。媒体拒绝与上下文过长按证据分别归类，分别保留受影响的模型 attempt，不能互相触发无限重试。

**7. Durable SkillService**

skills 是控制面的独立目录，description catalog 与 session 同级。采用 PostgreSQL 资源表同时保存 metadata、SKILL.md 和有界原始 archive；写入幂等回执与资源变更同事务提交，避免 metadata/blob 两份存储不一致。首版无需对象存储、版本历史库或内存替代数据库。

```python
@dataclass(frozen=True)
class SkillInfo:
    id: str
    name: str
    description: str
    revision: int
    sha256: str
    archive_bytes: int
    created_at: datetime
    updated_at: datetime

@dataclass(frozen=True)
class SkillDescription:
    id: str
    description: str

@dataclass(frozen=True)
class SkillDetail:
    info: SkillInfo
    skill_md: str

class SkillService:
    def __init__(self, pool: AsyncConnectionPool, *, schema: str = "public") -> None: ...
    async def initialize(self) -> None: ...
    async def create(self, archive: bytes, *, request_key: str) -> SkillInfo: ...
    async def update(
        self, skill_id: str, archive: bytes, *,
        expected_revision: int, request_key: str,
    ) -> SkillInfo: ...
    async def delete(
        self, skill_id: str, *, expected_revision: int, request_key: str,
    ) -> None: ...
    async def get(self, skill_id: str) -> SkillDetail: ...
    async def catalog(
        self, substring: str | None = None, *,
        after_id: str | None = None, limit: int | None = None,
    ) -> tuple[SkillDescription, ...]: ...
    async def download(
        self, skill_id: str, *, expected_revision: int | None = None,
    ) -> tuple[SkillInfo, bytes]: ...

def pack_skill(source_dir: Path, archive_path: Path) -> None: ...
def extract_skill(archive_path: Path, destination: Path) -> Path: ...

class InvalidSkill(Exception): ...
class SkillNotFound(Exception): ...
class SkillConflict(Exception): ...
class SkillTooLarge(Exception): ...
```

以上类型、函数、异常从 `kapy.skills` 导出。SkillService 构造器即 factory，构造无 I/O；initialize 是本模块幂等 schema 初始化入口，由 Gateway app lifespan 在接收请求前调用，不引入集中迁移框架。服务没有后台任务，不提供 start/aclose 或额外 open_skill_service；按照总设计师最终裁决借用 Gateway metadata pool，Gateway 负责其创建、打开和关闭，Skills 的事务和连接在单次方法内释放。所有 SQL 使用受信 schema 标识符限定，测试可注入独立 schema。

表 `skills`：`id uuid primary key`、`name text unique not null`、`description text not null`、`skill_md text not null`、`archive bytea not null`、`sha256 text not null`、`archive_bytes integer not null`、`revision bigint not null`、`created_at/updated_at timestamptz not null`。metadata 从归档解析，不能让调用方单独写一份与 SKILL.md 不一致的 description。create 生成 ID；update 整体替换归档及解析字段并加 revision，名称冲突报错；delete 真正删除当前资源。update/delete 用 expected_revision 避免并发覆盖。

表 `skill_requests` 保存 `request_key text primary key`、`method text`、`fingerprint text`、`result jsonb`、`created_at timestamptz`。对外统一 request_id UUID；Gateway 将受信主体标识与 request_id 稳定编码为内部 request_key 字符串（非空，UTF-8 最多 512 bytes），不增加 RequestKey 类型或外部 scope 字段。指纹包括方法、skill_id、expected_revision 和归档 SHA-256；先查幂等回执，再检查当前 revision。相同 key/指纹返回首次结果，同 key 不同参数报 SkillConflict；回执与资源 CRUD 同事务提交，避免客户端重试重复创建/更新。删除后保留回执，重试仍成功。request_key 解决重放，expected_revision 解决独立请求的并发覆盖，两者同时保留；调用者/creator/授权 metadata 由 Gateway 拥有。

catalog 稳定按 id 排序，在 id/name/description 上做大小写不敏感的字面 substring 匹配（使用参数化 `strpos`，不赋予 `%`、`_` 通配含义）。默认不筛选、不限量；after_id 为排他的 ID 下界，limit 为可选正数。get 返回 metadata 与完整 SKILL.md；download 在一次数据库读取中返回 metadata 和精确保存的 ZIP bytes，支持 revision 条件读取，避免传输元数据与包内容取到不同版本。旧 session 的 description snapshot 可继续存在，但已更新/删除的 skill 不提供历史版本；get/download 会返回当前版本或 NotFound，不能悄悄伪装成创建时的版本。

通用 skill 归档统一使用 ZIP，允许顶层直接 SKILL.md 或一个以 skill name 命名的根目录；CLI 上传目录时生成 ZIP。apply_patch 的上游 tar.gz 只在固定依赖获取逻辑中处理，不增加通用归档格式分支。按 [Agent Skills 规范](https://agentskills.io/specification) 校验 frontmatter 的 name/description 及已知可选字段，保留正文和资源，不执行脚本；allowed-tools 只作为 metadata，不能升级权限。

具体边界：压缩包最多 16 MiB，展开总量最多 128 MiB，最多 4096 个条目，单文件最多 32 MiB，SKILL.md 最多 64 KiB UTF-8。逐项计数并流式读取，限制同时作用于声明值和实际读出字节。拒绝绝对路径、`..`、反斜杠歧义、重复/冲突路径、symlink/hardlink、设备文件和特殊权限；只保留普通文件可执行位。YAML 使用 SafeLoader 并限制 alias，要求 mapping 和字符串字段。归档检查不依赖不受限 extractall。

CLI 复用 pack_skill/extract_skill 的全部归档策略。pack_skill 对普通 skill 目录生成带 name 根目录的 ZIP，保留普通文件执行位，拒绝链接、特殊文件和超限，不修改源目录；archive_path 必须不存在且位于源目录之外。extract_skill 再次检查 ZIP，写入新的临时目录，成功后发布到指定的不存在 destination，返回该 skill 根目录；失败清理临时目录，不覆盖已有目录。两个同步文件 helper 在异步调用侧交给有界线程执行。Skills 不将内容安装到控制服务工作目录；普通上传不注册 Python tool，执行脚本仍走获授权机器的 process 工具。

SkillService 不提供 chunk transfer API，也不提供第二套上传会话或传输任务。进程内 create/update 接收最多 16 MiB bytes，download 一次返回同版本的 `(SkillInfo, bytes)`；这些 bytes 从不直接放入 RPC。64 KiB chunk 由 Execution file.* 承载，Gateway 只负责传输编排和至多两次并行归档交换；归档验证、打包、解包和 PostgreSQL 存储全部复用 Skills。内存预算须计入最多两份有界归档及 driver/解压开销，不声称数据库 bytea 是端到端流式存储。

**8. Skill RPC、snapshot 与跨模块接入**

Gateway 暴露以下控制 RPC 并调用 SkillService。caller/session 的认证由代理连接上下文携带；表格内 session_id 只表达关联身份，不代替验证。SkillInfo 的 datetime 在 RPC 中为 UTC RFC 3339 字符串，其余字段原名不变。

| 方法 | params | result |
| --- | --- | --- |
| `skill.list` | `{query?:str,after_id?:str,limit?:int}` | `{items:list[{id:str,description:str}],next_after_id:str|null}` |
| `skill.get` | `{skill_id:str}` | `SkillInfo` |
| `skill.read` | `{skill_id:str}` | `{skill_id:str,markdown:str}` |
| `skill.create` | `{session_id:str,machine_id?:str,archive_path:str,request_id:str}` | `SkillInfo` |
| `skill.update` | `{skill_id:str,session_id:str,machine_id?:str,archive_path:str,expected_revision:int,request_id:str}` | `SkillInfo` |
| `skill.delete` | `{skill_id:str,expected_revision:int,request_id:str}` | `{skill_id:str,deleted:true}` |
| `skill.download` | `{skill_id:str,session_id:str,machine_id?:str,archive_path:str,expected_revision?:int,request_id:str}` | `{skill_id:str,archive_path:str,revision:int,sha256:str,archive_bytes:int}` |

request_id/session_id/skill_id 均为 UUID 字符串。Python 与 RPC 不要求同名：skill.list 调用 `catalog(query, after_id=..., limit=...)`，skill.get 投影 `get(...).info`，skill.read 投影 `get(...).skill_md`。CLI `skill upload` 在本地用 pack_skill 生成临时 ZIP，然后根据是否指定 skill_id 调用 create/update；没有单独 skill.upload RPC。未指定 machine_id 时由 Gateway 解析 session 默认机器。

传输采用 Execution 当前草案的 `file.pull/push/chunk/finish/abort`，不额外要求 Skills 的流式或 transfer_id 服务。upload：Gateway 发起 pull，检查声明 size≤16 MiB，逐个接收 decoded≤65,536 bytes 的 chunk，同时检查累计大小、连续 offset 和 hash；仅当 finish 确认完整成功后，调用 SkillService.create/update。download：先调用 SkillService.download 获取同一次读取的 info/bytes，再以 size 和 sha256 创建 push，逐个发送 decoded≤65,536 bytes 的 chunk，finish 验证成功后返回 RPC 结果。base64 后约 88 KiB 的单块消息满足 1 MiB RPC 上限；ZIP 完整内容从不作为单条 RPC params/result。

传输失败或取消时调用 file.abort 收束仍活动的传输，不能返回成功或发布半包。Gateway 从可信主体、request_id 和步骤稳定派生 Execution transfer_id；下载把实际 revision/hash 与传输绑定，同一 request_id 遇到已变更内容应明确冲突，不伪装重放首次成功结果。Skills 的只读 download 不额外接收 request_id 或 request_key。下载由 CLI 验证完整 ZIP hash 后用 extract_skill 安全提取，成功与失败均清理下载临时归档。上传只有确认成功才清理临时 ZIP；失败/未知结果保留同一 ZIP 和 request_id，使 CLI 可用同一请求重试，避免 create 已成功而 Gateway 尚未持久化 creator 时失去重放材料。放弃重试时由 CLI 明确清理。Gateway 不复制归档解析或 storage 逻辑。

skill.list 的 RPC limit 默认 100、范围 1–100；Gateway 向 service 多读一条判断是否有下一页，并可按 RPC frame 预算缩小本页，next_after_id 是本页最后一条 ID，有下一页才返回。它是实时目录遍历，不承诺跨页冻结并发修改。创建时目录直接调用 Python catalog，不分页、不截断；snapshot 超出 State JSON 大小限制时明确报告创建失败，首次 token 数未知。read 返回完整 SKILL.md，无需正文分页。

建议统一业务异常：InvalidSkill、SkillNotFound、SkillConflict、SkillTooLarge；Gateway 分别映射 JSON-RPC `-32602`、`-32004`、`-32009`、`-32020`，data.kind 分别为 invalid_skill、not_found、conflict、resource_limit，message 为安全的可读说明。编号最终由总设计师统一到共享 RPC 错误表。Execution 的 disconnect/unknown-operation 由其 RPC 错误类型透传成可读工具失败；取消不转换为普通错误。

创建 session 时，Gateway 调用 `runner.initial_state(instructions=..., skills=await skill_service.catalog())`，再把返回的 State.RunnerState 写入 SessionSpec.initial_state。State 创建事务保存实际使用的 snapshot 和 instruction；重复创建请求返回首次持久化结果，不使用重试时变化的目录。创建与 skill 更新并发时，以该次 catalog 查询读到的版本为准；无需锁住整个 skill 表。后续 CLI 的 list/get/read/download/upload 获取最新资源，输出作为当前 session 工具结果进入上下文，不改写旧 instruction，也不每轮热刷新所有 descriptions。

Gateway 的 HTTP JSON-RPC 入口复用 kapy.rpc.dispatch_json，Runner 只依赖 MachineCaller，不增加另一 codec。CLI 对 create/input receipt 的观察采用 State 已批准的 wait_submission(session_id,request_id,...)，不会消费 channel event；历史快照导出采用 export_history，不另建 runner receipt/history 服务。以上服务实现与导出归 State，Gateway 直接接入其已批准契约；Runner 的 read_history 仍使用原 RunContext 方法，不为这些控制面能力增加 context 字段。

Settings 显式映射：OPENAI_BASE_URL→RunnerConfig.base_url、OPENAI_API_KEY→api_key、OPENAI_MODEL→model；其余配置使用 KAPY_CONTEXT_WINDOW_TOKENS、KAPY_MAX_OUTPUT_TOKENS、KAPY_COMPRESSION_RATIO、KAPY_KEEP_RECENT_RATIO、KAPY_MEDIA_MAX_BYTES。模块自身不读取环境。Gateway 打开自有 HTTP client 与借给 Skills/AgentPayloadStore 的 metadata pool；app lifespan 分别调用各模块自己的迁移/initialize，构造服务并提供 authorize_wait，再启动 State，不引入集中迁移框架。State 单独拥有自己的 pool、Valkey 和迁移；schema 由总设计师统一映射，不假设 State 使用 public。关闭先停止并等待 State 的 runner 调用，再关闭 Gateway 自有资源。

对 State 直接使用其 RunContext/RunResult/checkpoint，媒体 metadata 与外置上下文引用均放既有 opaque JSON，不增加 State API。对 Execution 采用第 3 节完整 RPC、稳定 process/transfer UUID、file.finish 一致性检查及文件重定向 stdin；不需要额外扩展。对 Gateway 的需求是注入 client/config/MachineCaller/借用 metadata pool/AgentPayloadStore/authorize_wait、技能传输和 snapshot 接入，并在 State 完成 session 删除后持久重试 delete_session 清理。共享 pyproject、uv.lock、compose、README 由总设计师维护；本模块不新增 tiktoken 依赖，也不使用已存在的传递依赖计数。

未来对应 tests 使用模型/Telegram mocks；machine 调研和进程/文件测试只在专用 Docker 容器执行，按用户要求采用 best effort 后代清理，无 cgroup 前提。涉及持久化时使用总设计师提供且已 healthy 的 PostgreSQL/Valkey，每次生成独立 schema、key namespace 与临时 Execution XDG 根，清理仅限本次创建的资源。禁止重启共用服务、flush 共用 Valkey 或删除其他 scope 的数据；恢复场景使用独立控制进程/schema 或专属可丢弃服务。Runner 不直接依赖 Valkey。Agent、Compression、Media、Plugins、Skills 的模块证据由本 senior 提供；跨模块组合验收由总设计师组织。已读取主分支 scripts/check_provider.py，确认其使用 result.usage.requests；live 结果由总设计师提供，本 senior 不读取主目录 `.env`，不发送真实 Telegram 消息。此次只交最终修订方案，保留原先已完成的单次简化审查，不再开新 review；等待总设计师批准后才由本 senior 负责 cmd-impl 和自己的分阶段 review 子代理。
