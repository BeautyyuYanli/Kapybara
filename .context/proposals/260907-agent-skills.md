# Kapy v2：Agent Runner 与 Skills 方案

本方案依据 `kapy_v2.md`、`docs/architecture.md`，以及 main 提交 `61af09e` 的 `docs/acceptance.md` 和 `.context/delivery.md`，范围为 `src/kapy/agent/`、`src/kapy/skills/` 及对应 tests。验收清单描述后续需要提供的证据，不代表这些检查已通过。只提交方案；公共接口经总设计师统一批准后，才进入 cmd-impl。

**1. 实现边界与实际 API**

State 管理 session、运行串行性、输入领取、事件订阅、waiting 转换、历史和输出游标；Runner 只执行一次从唤醒到等待的推理过程。Gateway 创建服务、注入配置和 MachineCaller，Execution 执行进程及文件操作。不同 session 可以并发，不引入另一套 session/event 或 Pydantic AI 的持久化后端。

已执行 `uv sync --locked`，实际安装 Python 3.14.4、pydantic-ai-slim 2.40.0、openai 3.8.0、httpx2 2.12.0。以下结论来自当前 `.venv/lib/python3.14/site-packages/pydantic_ai/` 源码及仅使用 dummy key 的本地 mock：

- `Agent` 使用 `capabilities`；历史处理使用 `ProcessHistory` / `AbstractCapability.before_model_request`，不能照搬旧版 `history_processors=`。
- `Agent.run_stream_events(...)` 返回异步上下文管理器；`RunContext.enqueue(..., priority="asap")` 可在工具/模型边界注入 steer。框架会在自然结束前处理已注入的输入。
- `Tool.from_schema(function, name, description, json_schema, takes_ctx=False, sequential=False, args_validator=None)` 跳过 schema 校验；插件必须用自己的 Pydantic 参数模型验证。
- `ToolReturn.return_value` 可直接含 `BinaryContent`。Chat 模型会将其映射成配对的文字 tool message 和附加的媒体 user message；不用 `ToolReturn.content` 人工拆出另一条媒体输入。
- `ProcessHistory` 的结果会替换当前运行的消息历史，不能把最终 `new_messages()` 当作未压缩的历史归档。
- Chat 模型没有实现服务端 `count_tokens`；本地 profile 对此模型也未给出 context window。`ModelMessagesTypeAdapter` 是实际可用的消息序列化边界。
- 流式 mock 中，普通 `wrap_model_request` 没接住延迟启动时的媒体 400；在 Runner 外层捕获、替换最近请求的媒体返回并续跑成功。共三次模型请求，`read_media` 只执行一次；重试保留同一 `tool_call_id`。

模型构造使用实际安装的 `OpenAIChatModel("gpt-5.6-luna", provider=OpenAIProvider(base_url=..., api_key=..., http_client=...))`。由 Gateway 显式传入 base/key，不在 agent 模块自动读取 `.env`。首版采用 Chat Completions，完整上下文由本地传入。OpenAI 官方给出的该模型窗口为 1,050,000；自定义 endpoint 的实际限制可能不同，因此窗口必须可以覆盖配置。[模型资料](https://developers.openai.com/api/docs/models/gpt-5.6-luna)

**2. Runner 导出与 State 契约**

以下是待总设计师与 State 统一的精确接口建议，类型从 `kapy.agent` 导出。`JsonObject = dict[str, JsonValue]`，其中 `JsonValue` 使用 Pydantic 的 JSON 类型；所有 ID 在 Python 和 RPC 中均使用字符串。

```python
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
    media_reserve_tokens: int = 32_768

@dataclass(frozen=True)
class RunnerInput:
    input_id: str
    text: str

@dataclass(frozen=True)
class RunResult:
    output: str
    waiting_ids: tuple[str, ...]
    history: JsonObject

class MachineCaller(Protocol):
    async def call(
        self, machine_id: str, method: str, params: JsonObject,
    ) -> JsonValue: ...

class RunnerContext(Protocol):
    session_id: str
    turn_id: str
    default_machine_id: str | None
    instructions: str

    async def take_steer(self) -> tuple[RunnerInput, ...]: ...
    async def emit(self, event: JsonObject) -> None: ...
    async def checkpoint(
        self, *, checkpoint_id: str, history: JsonObject,
        messages_json: bytes, consumed_input_ids: tuple[str, ...],
    ) -> None: ...

class Runner:
    def __init__(
        self, config: RunnerConfig, machine_caller: MachineCaller, *,
        http_client: httpx2.AsyncClient,
        plugins: Sequence[ScriptTool] = (),
    ) -> None: ...

    async def __call__(
        self, context: RunnerContext, inputs: Sequence[RunnerInput],
        history: JsonObject | None,
    ) -> RunResult: ...
```

`Runner` 必含内置工具与 apply_patch；`plugins` 只添加自定义工具，重名立即报错。Gateway 持有并关闭共享 HTTP client；Runner 借用它，绝不关闭注入资源。Pydantic Agent、capabilities 和每次运行的可变数据在 `__call__` 内创建，避免多个 session 共享可变消息、媒体修复标记或等待结果。

`instructions` 是 State 保存的完整创建时 instruction，包含基础 prompt 和当时全量 skill descriptions。恢复时必须重用它；Runner 不每轮重新读取 catalog。机器存在性与关联鉴权统一由 Gateway 的 MachineCaller 处理，Runner 将未关联/不存在错误反馈为可纠正的 tool response。没有默认机器时，工具省略 machine_id 会得到可纠正的参数错误。

`take_steer` 是 State 提供的持久化领取操作：同一运行不重复领取；在包含该输入的 checkpoint 成功之前不最终确认消费。Runner 在模型节点前、工具节点前后、准备结束前领取并 enqueue；queue 输入由 State 留到下次唤醒。State 在 waiting 提交前再次原子检查新到输入，解决最后一次轮询后的竞态。

`checkpoint` 原子执行：追加尚未归档的原始完整消息、保存 Runner 的上下文投影、确认已写入投影的输入 ID。`checkpoint_id = turn_id + ":" + 单调步骤号`，步骤号保存在 history 中，State 以 session_id + checkpoint_id 幂等。消息在模型响应完整落定后、工具调用执行前、工具结果落定后、压缩前提交；流式碎片先走 emit，不能当作可执行的完整 tool call。

`messages_json` 是 `ModelMessagesTypeAdapter.dump_json` 的新增完整消息数组，空数组使用 `b"[]"`。State 将它作为受信消息编码持久化，不对数据库行做 ORM 二次 validation。State 原始历史不因压缩或媒体替换而覆盖；Runner context 可包含媒体修复后的投影，恢复时按消息 codec 解码。

`history` 为 Runner 拥有的 JSON 文档，State 不解释内容。v1 形状如下，messages 是上述 adapter 的 JSON 数组；同轮恢复保留 checkpoint_seq 和 turn_id：

```json
{
  "version": 1,
  "checkpoint_seq": 12,
  "media_fallback_call_ids": ["read-media-call-1"],
  "cycles": [
    {"turn_id": "t1", "closed": true, "level": 1, "messages": []},
    {"turn_id": "t2", "closed": false, "level": 0, "messages": []}
  ]
}
```

cycle 表示两次 waiting 之间的上下文段；只有最后一段允许未关闭。cycle 中空 messages 仅用于展示形状，实际投影必须保留当前请求。media_fallback_call_ids 只保留投影中仍存在的已修复调用，随对应区间丢弃，防止恢复时重试预算重置。`turn_id` 在恢复同一轮时不变；Pydantic 的 run_id 可因媒体重试而更新。所有机器副作用携带由 `session_id/turn_id/tool_call_id/子操作` 确定的 operation_id。恢复到未完成 tool call 时，Execution 必须返回同一操作或明确 unknown；Runner 不擅自换 ID 重跑。已完成工具结果必须写入下一模型请求前的 checkpoint。

`emit` 接收下列联合形状，State 负责持久化、输出游标和前端消费：

```text
{"kind":"model_delta","attempt_id":str,"part_index":int,"text":str}
{"kind":"tool_call","attempt_id":str,"tool_call_id":str,"name":str,"args":JsonObject}
{"kind":"tool_result","attempt_id":str,"tool_call_id":str,"result":JsonValue}
{"kind":"attempt_failed","attempt_id":str,"code":str,"message":str}
```

工具结果中的大文件/媒体使用路径、字节数、类型等摘要，不广播 base64。最终完整消息及输出由 checkpoint/RunResult 统一提交。attempt_failed 使前端知道已流出的片段属于失败尝试，不能拼入随后成功的最终回答。State 在最终事务中保存 RunResult、进入 waiting、订阅 own input channel 加去重后的 waiting_ids，并发出 completion。Runner 不发 completion，不自行等待 channel。错误和取消向 State 抛出，不能伪造正常完成。

**3. 内置工具与 Execution RPC 需求**

模型侧工具的参数均从 agent 的角度描述。所有机器工具有 `machine_id: str | None = None`；RPC 的 session_id、operation_id 和 session 凭证由 Runner/Gateway 注入，不允许模型填写。路径由 Execution 根据目标 session cwd 解析。stdio 保持完整输出，PTY 保持 8192-byte 窗口；返回超时不意味着杀进程。

```python
async def process_run(
    command: str, *, machine_id: str | None = None,
    cwd: str | None = None,
) -> JsonObject: ...

async def process_start(
    command: str, *, machine_id: str | None = None,
    cwd: str | None = None, wait_ms: int = 1000,
) -> JsonObject: ...

async def process_read(
    process_id: str, *, machine_id: str | None = None,
    cursor: int | None = None, wait_ms: int = 1000,
) -> JsonObject: ...

async def process_write(
    process_id: str, input: str, *, machine_id: str | None = None,
    cursor: int | None = None, wait_ms: int = 1000,
) -> JsonObject: ...

async def process_kill(
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

普通命令由 shell 解释；插件走 argv + stdin。所有 process 工具默认 sequential，避免同批调用的输入、读取、patch 相互交错。`process_write` 可传 `\u0003` 中断前台程序；`process_kill` 清理整个 PTY 进程树。`process_read` 的 cursor 由模型沿用，掉出缓冲窗口时明确给出 truncated 信息。

`wait` 使用 `ToolOutput(wait, name="wait")` 与文字输出并存；只有 `wait_for` 一个数组参数，无额外 output/reason 字段。`WaitRequest` 是私有结果类型，仅保存去重后的 waiting ids。选用 `end_strategy="exhaustive"` 并对工具设 sequential，使同一响应里的工具按顺序落定、返回全部 call 的结果后结束。自然文字结束返回空 waiting_ids，State 始终添加 own input channel。模型在 wait 前产生的文字作为 output；没有文字则 output 为空字符串。

建议 Execution 定稿时采用以下形状，`S` 表示 `{session_id:str}`，`M` 为 `S + {operation_id:str}`，所有结果由 JSON-RPC 2.0 result 包装：

| 方法 | params | result |
| --- | --- | --- |
| `session.ensure` | `S` | `{cwd:str, platform:"linux", arch:"x86_64"|"aarch64"}` |
| `process.run` | `M + {argv:list[str], stdin_b64:str|null, cwd:str|null}` | `{process_id:str, exit_code:int|null, stdout:Output, stderr:Output}` |
| `process.start` | `M + {argv:list[str], cwd:str|null, wait_ms:int}` | `PtyResult` |
| `process.read` | `S + {process_id:str,cursor:int|null,wait_ms:int}` | `PtyResult` |
| `process.write` | `M + {process_id:str,input_b64:str,cursor:int|null,wait_ms:int}` | `PtyResult` |
| `process.kill` | `M + {process_id:str}` | `{process_id:str,state:"exited",exit_code:int|null}` |
| `file.stat` | `S + {path:str}` | `{size:int,version:str,media_type:str|null}` |
| `file.read` | `S + {path:str,offset:int,limit:int,version:str|null}` | `{data_b64:str,next_offset:int,eof:bool,version:str}` |
| `file.write` | `M + {path:str,offset:int,data_b64:str,final:bool,mode:int|null}` | `{next_offset:int,complete:bool}` |

`Output = {text:str}` 或 `{path:str,bytes:int}`。stdio 全量内容超过 RPC frame 时由 Execution 写 spool 并返回引用，不截断；Runner 给模型最多 64 KiB 的单次展示，明确保留完整文件路径供分块读取，State 保存引用。这不改变 Execution stdio 的完整输出语义。

`PtyResult = {process_id:str,state:"running"|"exited",output:str,cursor:int,truncated:bool,exit_code:int|null,timed_out:bool}`。PTY 的字节窗口和 UTF-8 解码由 Execution 统一处理。stdio 输入过大时复用分块 file.write 写临时 stdin 文件，再让脚本从文件读取，不能放大单个 RPC frame。

file.read 原始块上限 64 KiB；file.write 采用 staging 后 final 原子替换，operation_id 标识整次传输，重复 offset+相同数据可重试，不同数据冲突。version 防止 read_media 多块拼接出两个版本的文件。普通 file_read 解码失败时返回二进制提示；file_write 把文字编码为 UTF-8 并分块。已有连接内的媒体与 skill 传输只依赖该接口；presigned URL 支持由 Execution/Gateway 提供，Runner 不再实现对象存储客户端。

**4. 脚本插件与 apply_patch**

不增加远程插件 registry、动态 Python 加载或第二套进程管理器。插件是启动时显式注入的 Python 定义，参数和 description 可定制，执行结果统一走 process.run。

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

第一个插件导出模型工具 `apply_patch(patch: str, machine_id: str | None = None)`，通过固定 argv 指向目标机器的 apply_patch 可执行文件，patch 原文走 stdin；保持 LF，不把 patch 放进 shell heredoc。非零退出码按普通工具结果反馈，不假定多文件 patch 失败时已回滚。

采用已核对的 [codex-apply-patch 发布包 rust-v0.153.4](https://github.com/BeautyyuYanli/codex-apply-patch/releases/tag/rust-v0.153.4)。Linux x86_64 tar.gz 的 SHA-256 为 `d2b6db33f1ebdbca9691237287fe25ee4fe53b90f0255a7e3cfb82b70c721567`；aarch64 为 `0b518abbbd75016f615a6164c45f1a7571c3be6d6d55559f3019ee4913cfe0b5`。包含 SKILL.md、binary、LICENSE、NOTICE、SOURCE 和第三方许可。

工具 description 以该发布包完整 [SKILL.md](https://github.com/BeautyyuYanli/codex-apply-patch/blob/8639ac2d93442bcec5631b693b4ed7c0144422b7/SKILL.md) 为来源。它的原始传输约定是 FREEFORM，与 Pydantic 普通 function tool 的 JSON 参数不一致：生成 description 时只改写传输约定，说明外层是参数对象、patch 字段内容是原始补丁；保留其语法、示例和行为说明。catalog 的 description 则使用 frontmatter 的简短 description，二者用途不同。该 revision 的上游 CLI tests 明确覆盖无 argv 补丁参数时通过 stdin 输入。

批准后在本模块提供同步生成器：从固定 release 获取并校验 bundle，生成随包的 SKILL 来源与 manifest；生成文件只通过该工具更新。运行时按 manifest 获取对应 Linux binary，校验后经 file.write 安装到 session cwd 下的受管工具目录，保留 executable bit 和许可文件；同一 hash 的安装复用。缺失平台或下载失败返回明确工具错误，不换成自制 patch。SKILL.md 的路径约束是给模型的说明，不将它误称为 binary 提供的文件系统沙箱。

**5. 媒体拒绝后的恢复**

read_media 经 file.stat 和分块 file.read 取得一致版本，累计上限 20 MiB；不能读取时返回普通文字错误。可识别的媒体构造 `ToolReturn(return_value=[文字说明, BinaryContent(...)], metadata=来源信息)`，来源包含 machine_id、path、media_type 和 tool_call_id。不能识别的类型直接返回文字，不把文件名后缀当作模型能力保证。

每个模型请求前保存可恢复的完整投影，其中已包含此前所有 tool call/return。模型拒绝媒体时，Runner 外层对该请求的 read_media 返回进行以下处理：

1. 根据工具归属和 media part 定位受影响的 tool_call_id；删除这些返回中的媒体，改为包含 provider 错误代码及文字的 tool response。多个媒体无法精确归因时替换该请求中全部 read_media 媒体，并明确说明此次请求被拒绝，不能声称每个文件都不受支持。
2. 保留 tool name、tool_call_id、同批其他结果和输入次序；持久化修复后的投影及 attempt_failed，再用 `user_prompt=None` 和该请求历史创建新的 Pydantic run。不会再次调用进程、读取媒体或消费同一输入。
3. 对同一批媒体只做一次文字重试。后续模型主动调用新的 read_media 是新的工具调用；已拒绝的旧媒体不能在下一轮从原始历史重新注入。

HTTP 400/422 必须带媒体字段/类型/解码相关错误证据才归入此分支。模型适配器对已知媒体的序列化不支持也转换为文字。鉴权、限流、普通网络错误、上下文过大不假称媒体拒绝；重试后的错误交 State 处理。provider 错误文字限 8 KiB，去除认证信息、URL query 和大块 base64，保留诊断内容。中途已流出的失败尝试通过 attempt_id 标记，不执行不完整的 tool arguments。

**6. 分层 context compression**

压缩仅改变 Runner 的模型上下文投影。State 原始 PostgreSQL 历史完整保留，基础 instruction 说明可用 `kapy control history` 查找旧记录；不引入向量库、LLM 总结、摘要树或独立检索系统。[Pydantic 消息历史说明](https://pydantic.dev/docs/ai/core-concepts/message-history/)

预算 `C = context_window_tokens`，在每个模型请求前估算 instruction、catalog、tool schemas 和消息总量；达到 `0.70 * C` 时触发。文本用已锁定的 tiktoken 0.14.0（当前 gpt-5 前缀映射 o200k_base）计数，加消息结构开销和 10% 余量；结合最近真实 input_tokens 向上校准。媒体不把 base64 字符串算作普通文本，首版每个媒体预留 32,768 tokens，并允许 Gateway 按 endpoint 调整 `media_reserve_tokens`。这是估计阈值，不冒称服务端精确 token 数。输出另预留 max_output_tokens；不得仅用上一轮 usage 或消息条数判断新请求。

从最新消息向前取约当前可压缩历史估计 token 的 10%，按完整 user/model/tool 交互块向前扩展，维持 level 0；额外保护当前输入与尚未配对完成的工具链。固定 instruction 和创建时 catalog 不降级。closed cycle 跨过保留边界时整段保护，允许超过 10%；当前未关闭 cycle 允许旧的完整交互块只降到 level 1。

| 级别 | 模型看见的内容 |
| --- | --- |
| 0 | 原始消息及结果；已拒绝媒体使用文字修复投影；当前 cycle 的局部 level 1 例外见下文 |
| 1 | 保留输入、assistant 文字及工具名称/参数；每个工具结果只保留同一 ID 的明确省略占位，不保留原 response 内容或媒体 |
| 2 | 只保留这一 waiting 区间的全部输入（含 steer/event 输入）与最终 output，重建普通 user/assistant 消息 |
| 丢弃 | 从投影删除整个区间，原始历史仍可查询 |

一次压缩 sweep 按开始时的 level 同步推进旧区间：0→1、1→2、2→丢弃，每区间每次只降一级；重估后仍超阈值则继续 sweep，最多三轮即可清空所有可降级旧区间。最新保护集合在这次压缩中固定，避免反复计算后吃掉最新内容。level 1 用占位 tool return 满足 OpenAI 配对约束，不能直接删除 return 留悬空 call。

压缩的最小单位包含 assistant 中的整批 tool calls、对应全部 tool returns 和 retry parts；不截断 JSON 参数，不产生悬空/重复 tool_call_id。level 2 或丢弃时删除整组工具协议消息；最后仍必须有当前 ModelRequest。原始消息在压缩前已 checkpoint，压缩 level 与投影同步持久化，所以重启不会将旧消息重新升到 0。

当前尚未结束的 cycle 中，已完成且不属于最新保护区的交互块也参与 0→1；占位结果用 `ToolReturnPart.metadata` 的 `kapy_compression: 1` 记录，cycle.level 暂仍为 0，直到整段完成 0→1。当前 cycle 不做 1→2 或丢弃，不能提前捏造等待输出。若固定 instruction、最新保护区和保留下来的 call 参数仍无法留出输出空间，报告明确 ContextBudgetExceeded 给 State，保留输入和历史，不无界重试或静默裁剪用户输入。大工具输出和媒体的单次展示限额可降低这种情况发生的频率。

**7. Durable SkillService**

skills 是控制面的独立目录，description catalog 与 session 同级。采用 PostgreSQL 单表同时保存 metadata、SKILL.md 和有界原始 archive，单事务更新，避免 metadata/blob 两份存储不一致。首版无需对象存储、版本历史库或内存替代数据库。

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
    name: str
    description: str
    revision: int

@dataclass(frozen=True)
class SkillDetail:
    info: SkillInfo
    skill_md: str

class SkillService:
    def __init__(self, pool: AsyncConnectionPool, *, schema: str = "public") -> None: ...
    async def initialize(self) -> None: ...
    async def create(self, archive: bytes) -> SkillInfo: ...
    async def update(
        self, skill_id: str, archive: bytes, *, expected_revision: int,
    ) -> SkillInfo: ...
    async def delete(self, skill_id: str, *, expected_revision: int) -> None: ...
    async def get(self, skill_id: str) -> SkillDetail: ...
    async def catalog(
        self, substring: str | None = None, *,
        after_id: str | None = None, limit: int | None = None,
    ) -> tuple[SkillDescription, ...]: ...
    async def download(
        self, skill_id: str, *, expected_revision: int | None = None,
    ) -> tuple[SkillInfo, bytes]: ...

def extract_skill_archive(archive: bytes, destination: Path) -> Path: ...
```

以上类型及函数从 `kapy.skills` 导出。`initialize` 只创建本模块表；Gateway/State 的启动编排统一调用，模块不自己创建连接池或后台任务。pool 生命周期属于 Gateway；所有 SQL 使用受信 schema 标识符限定，测试可注入独立 schema。

表 `skills`：`id uuid primary key`、`name text unique not null`、`description text not null`、`skill_md text not null`、`archive bytea not null`、`sha256 text not null`、`archive_bytes integer not null`、`revision bigint not null`、`created_at/updated_at timestamptz not null`。metadata 从归档解析，不能让调用方单独写一份与 SKILL.md 不一致的 description。create 生成 ID；update 整体替换归档及解析字段并加 revision，名称冲突报错；delete 真正删除当前资源。update/delete 用 expected_revision 避免并发覆盖。

catalog 稳定按 id 排序，在 id/name/description 上做大小写不敏感的字面 substring 匹配（使用参数化 `strpos`，不赋予 `%`、`_` 通配含义）。默认不筛选、不限量；after_id 为排他的 ID 下界，limit 为可选正数。get 返回 metadata 与完整 SKILL.md；download 在一次数据库读取中返回 metadata 和精确保存的 ZIP bytes，支持 revision 条件读取，避免传输元数据与包内容取到不同版本。旧 session 的 description snapshot 可继续存在，但已更新/删除的 skill 不提供历史版本；get/download 会返回当前版本或 NotFound，不能悄悄伪装成创建时的版本。

通用 skill 归档统一使用 ZIP，允许顶层直接 SKILL.md 或一个以 skill name 命名的根目录；CLI 上传目录时生成 ZIP。apply_patch 的上游 tar.gz 只在固定依赖获取逻辑中处理，不增加通用归档格式分支。按 [Agent Skills 规范](https://agentskills.io/specification) 校验 frontmatter 的 name/description 及已知可选字段，保留正文和资源，不执行脚本；allowed-tools 只作为 metadata，不能升级权限。

具体边界：压缩包最多 32 MiB，展开总量最多 128 MiB，最多 4096 个条目，单文件最多 32 MiB，SKILL.md 最多 64 KiB UTF-8。逐项计数并流式读取，限制同时作用于声明值和实际读出字节。拒绝绝对路径、`..`、反斜杠歧义、重复/冲突路径、symlink/hardlink、设备文件和特殊权限；只保留普通文件可执行位。YAML 使用 SafeLoader 并限制 alias，要求 mapping 和字符串字段。归档检查不依赖不受限 extractall。

CLI 使用同包的 extract_skill_archive 再次检查归档，写入新的临时目录，成功后发布到指定的不存在目标；失败清理临时目录，不覆盖已有目录。不将 skill 安装到控制服务工作目录。普通 skill 上传不自动注册 Python tool；执行归档脚本只能通过已授权机器的 process 工具。

**8. Skill RPC、snapshot 与跨模块接入**

Gateway 暴露以下控制 RPC 并调用 SkillService。caller/session 的认证由代理连接上下文携带；表格内 session_id 只表达关联身份，不代替验证。SkillInfo 的 datetime 在 RPC 中为 UTC RFC 3339 字符串，其余字段原名不变。

| 方法 | params | result |
| --- | --- | --- |
| `skill.catalog` | `{substring?:str,after_id?:str,limit?:int}` | `{items:list[SkillDescription],next_after_id:str|null}` |
| `skill.get` | `{skill_id:str}` | `{info:SkillInfo,skill_md:str}` |
| `skill.upload` | `{session_id:str,machine_id?:str,path:str,skill_id?:str,expected_revision?:int}` | `{info:SkillInfo}` |
| `skill.delete` | `{skill_id:str,expected_revision:int}` | `{deleted:true}` |
| `skill.download` | `{skill_id:str,session_id:str,machine_id?:str,path:str,expected_revision?:int}` | `{path:str,revision:int,sha256:str,archive_bytes:int}` |

skill.upload 的 path 为执行机器上的归档；无 skill_id 时 create，指定 skill_id 时 update 并要求 expected_revision。Gateway 按归档上限使用现有 file 接口拉取，不在 JSON-RPC 内塞整包 base64。CLI 上传目录时先本地打包到 session 临时目录。skill.download 由 Gateway 获取归档后复用 file.write 分块推送到 path；CLI 检查结果 hash、提取到用户指定目录并删除临时归档。Gateway 在下载传输期间持有同一次读取的 bytes/info，revision 条件不匹配时先报冲突，不能把两个版本混装。

skill.catalog 的 RPC limit 默认 100、范围 1–100；Gateway 向 service 多读一条判断是否有下一页，并可按 RPC frame 预算缩小本页，next_after_id 是本页最后一条 ID，有下一页才返回。它是实时目录遍历，不承诺跨页冻结并发修改。State 的创建时 catalog 直接调用 Python service，不分页、不截断；固定 instruction 超出模型预算时明确报告创建失败。

建议统一业务异常：InvalidSkill、SkillNotFound、SkillConflict、SkillTooLarge；Gateway 分别映射 JSON-RPC `-32602`、`-32004`、`-32009`、`-32013`，message 为可读说明，data 为 `{kind:str}`。Execution 的 disconnect/unknown-operation 由其 RPC 错误类型透传成可读工具失败；取消不转换为普通错误。

创建 session 时，Gateway 读取完整 catalog，将 description 列表与基础 prompt 交 State，在 session 创建事务中保存实际使用的 snapshot 和 instruction。创建与 skill 更新并发时，以该次 catalog 查询读到的版本为准；无需锁住整个 skill 表。后续 CLI 的 catalog/get/download/upload 获取最新资源，输出作为当前 session 工具结果进入上下文，不改写旧 instruction，也不每轮热刷新所有 descriptions。

对 State 的需求是上述 context/checkpoint、输入领取、attempt 事件、按 waiting 划分的历史及最终原子转换；对 Execution 的需求是精确的 process/file RPC、stdin/argv、幂等副作用和 file version；对 Gateway 的需求是注入 client/config/MachineCaller/pool、技能传输和 snapshot 接入。共享 pyproject、uv.lock、compose、README 由总设计师统一维护；请总设计师将当前锁中已存在的 tiktoken 声明为直接依赖，版本继续通过 uv 管理，本 senior 不修改共享配置。

未来对应 tests 使用模型/Telegram mocks；涉及持久化时使用总设计师提供且已 healthy 的 PostgreSQL/Valkey，每次生成独立 schema、key namespace 与临时 Execution XDG 根，清理仅限本次创建的资源。禁止重启共用服务、flush 共用 Valkey 或删除其他 scope 的数据；恢复场景使用独立控制进程/schema 或专属可丢弃服务。Runner 不直接依赖 Valkey。Agent、Compression、Media、Plugins、Skills 的模块证据由本 senior 提供；跨模块负载与组合验收由总设计师组织，不编造吞吐目标。live 模型调用由总设计师单独使用主目录配置完成；本 senior 不读取主目录 `.env`，不发送真实 Telegram 消息。公共类型、RPC 名称、限额与持久化边界均由总设计师读完各 owner 方案后统一批准；直接沟通只用于澄清接口需求，不授权实现或管理其他 owner 的下级。
