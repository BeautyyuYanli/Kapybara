# Session 模型配置、三种协议与 agent 使用说明

以 `kapy_v2.md` 和用户最新补充为准，保留控制面/执行面、State session、插件工具及前端接口的职责。模型连接从服务级配置移到 session API；默认协议为 OpenAI Responses，另支持 OpenAI Chat Completions 与 Google AI Studio 原生 Gemini API。

## Session API

`session.create` 与 `session.update` 的 `config.model` 改为结构化对象；`config.instructions` 保持字符串，其他 session 字段及 UUID 幂等语义不变。

```python
type ModelType = Literal["openai_responses", "openai_chat", "google_ai_studio"]

class ModelConfig(BaseModel):
    type: ModelType = "openai_responses"
    base_url: str | None = None
    name: str | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int = 16_384
    api_key: SecretStr | None = None
```

省略地址时采用该协议的标准地址，解析后保存实际地址；OpenAI 地址是含 `/v1` 的 API base，Google 地址是 Gemini API host/base，由 SDK 添加版本与资源路径。允许自定义地址；拒绝 URL 内嵌凭据、query 和 fragment。key 必须显式提供或来自已授权的 session 继承，不从 SDK 的环境变量兜底。

session 身份创建子 session 时默认继承已认证调用者的模型配置；前端与 operator 提交配置，没有隐式服务端默认模型，也不增加任意 session 间复制凭据的参数。模型对象中的显式字段覆盖继承值；新连接的默认 type 是 Responses，继承时省略 type 保留来源协议。修改 name 而未明确提供 window 时重新解析对应模型的窗口，不能沿用另一模型的数值；修改 type/base_url 时必须明确提供 key，不能把旧 key 自动发往新地址。更新当前 session 时省略 key 保留该 session 的现有 key，其余公开配置按完整替换语义处理。公开结果只返回已解析的模型配置与 `has_api_key`，不返回 key；返回字段可直接用于修改其他公开设置，`has_api_key` 只读。

新增只读 `session.models`：

```text
params = {session_id?: UUID, model?: ModelConfig, page_token?: str, limit?: int}
result = {items: [{name, context_window_tokens: int|null,
                  max_output_tokens: int|null}], next_page_token: str|null}
```

提供 session_id 时先执行现有 session 授权，再借用其连接；显式连接覆盖遵循相同的 key 规则。该调用不创建 session、不修改默认设置。OpenAI 查询 `/models`；Google 使用原生 models list/get，筛选支持 `generateContent` 的模型。使用有界分页、响应体和超时，不遍历无界模型目录。创建时名称缺失，只有探测得到唯一可用候选且没有未读页时才自动选中，否则返回可纠正的“需要指定模型名”。名称、窗口均明确时无需探测，兼容不开放 `/models` 的端点。

窗口仅取用户显式值或供应商返回的对应字段；OpenAI 标准 models 响应没有 context window，不依据模型名建立猜测表。Google 使用 `inputTokenLimit`，输出上限使用 `outputTokenLimit` 约束配置值；用户显式输入的窗口优先。规范化、继承和发现由 Gateway 的同一个模型配置解析入口完成；解析结果在首次持久写入时冻结，同一个 request_id 恢复不能因 models 列表变化而改用另一模型。Runner 每轮只读取已冻结配置，不重新探测。校验输出预算为正且小于窗口，未知窗口明确报参数错误。

## 凭据与持久边界

State 继续只保存非秘密 `config` 和 opaque RunnerState。Gateway 为 session 模型写入保存不可变 revision，公开模型配置与内部 revision 引用随 State 的 create/update 一次提交；每轮运行使用该版本，避免更新与 runner 读取不同步。

新增 Gateway 自有 `gateway_model_secrets(revision_id UUID PRIMARY KEY, session_id UUID NULL, api_key TEXT NOT NULL)`，revision_id 使用对应的 mutation request_id。原始 key 从通用 `gateway_requests.params/operation/result` 剥离，摘要仍覆盖完整输入，保证同 UUID 换 key 报冲突。请求预留与私有 key 写入在同一事务完成；恢复读取已冻结版本，不重新选择凭据。create 的 session_id 在 State 返回后补齐；启动门控仍等 Gateway 完成归属记录。继承为子 session 复制独立凭据版本，删除父 session 不破坏子 session。删除沿现有 cleanup，在 State 停止 runner 后清理该 session 的所有秘密版本。

内部 revision 不能由用户指定，也不作为可转让凭据；普通 create/get/list/update 结果、State 历史、模型提示、错误和日志均不包含 key。提供 key 的参数用 SecretStr 校验，验证错误不回显原始输入。无模型配置的旧 session 保留历史，可由 session.update 配置后继续；服务不持续读取旧部署模型变量代替 session 设置。

## 模型适配与运行

保留 `ModelBackend.create_model(name)` 与 `classify_error(error)` 的借用资源接口，增加明确工厂：

```python
type ModelBackendFactory = Callable[[ModelConfig, httpx2.AsyncClient], ModelBackend]

def create_model_backend(
    config: ModelConfig, http_client: httpx2.AsyncClient,
) -> ModelBackend: ...
```

工厂分别构造 Pydantic AI 的 `OpenAIResponsesModel/OpenAIChatModel/GoogleModel`，显式注入地址、key 和同一个借用 HTTP client。`create_app(..., model_backend_factory=...)` 允许替换适配器；不再使用单个服务级 endpoint/backend 决定所有 session。Gateway 每轮解析 session 的冻结模型设置并构造该轮 Runner，RunnerConfig 中的窗口、模型名和输出预算来自该 session，实例之间不共享可变配置。`Runner.initial_state` 改为不依赖实例的静态入口，继续冻结用户 instructions 和 skill catalog。

Responses 使用 `store=False`、完整本地历史，不启用 previous_response_id/conversation 或供应商自动截断。禁用依赖原始 Responses item ID 的历史回放，保留实际需要的 reasoning 加密内容与工具调用配对；同端点恢复不重做已完成工具。Google 保留 SDK 的 thought signature 及对应工具协议。类型、端点或模型改变时，仅在模型上下文投影中移除原供应商专有 ID、签名与不透明推理项，保留用户/assistant 正文、媒体和成对工具记录；原始历史不改写。模型身份变化后清掉旧请求 usage 的压缩触发依据，避免另一模型的数值触发新窗口压缩。

三种协议的实际 usage 归一到现有计数；继续仅使用 provider API 最新单次响应 usage，不添加本地 tokenizer。错误分类按各 SDK 的真实字段处理 context/media；其他错误安全地交 State，不能将鉴权或限流伪装成媒体拒绝。工具集合仍只有既定进程工具、read_media、wait 和注入的 ScriptTool；apply_patch 继续走普通插件入口。

依赖使用 `pydantic-ai-slim[google,openai]`。当前 google-genai 的约束要求 websockets<17，因此采用 16.x；所有版本由 uv 解析与生成 lock，不手改锁文件。

## CLI 与 Telegram

CLI 的 session create/update 接受完整模型 JSON 配置文件，保留 `--model` 名称快捷参数，并提供 key-file/key-env 输入；不为每个模型配置字段再增加一套旗标。key 从文件或指定环境变量读取后放进 API 请求，不打印。`session models` 访问同一发现接口；递归 CLI 创建默认继承当前 session。更新的其他 session 字段继续完整替换，不引入第二套控制通道。

Telegram `/model` 支持结构化模型设置，`/models` 展示同一发现接口的结果；模型配置保存为该 chat/topic 的前端设置，`/new` 使用保存值。`/model NAME` 在已配置连接上只改名称并重新处理窗口。配置命令不会作为普通 prompt 进入 session；`/settings`、命令回执和错误均隐藏 key。待处理设置输入及保存配置属于 Telegram 私有持久数据，key 不进入 delivery projection 或 session 历史。更新活动 session 仍要求 waiting；忙碌时保存供下一 `/new`，不热改正在执行的请求。

服务 Settings、Compose 与配置示例移除模型端点/key/type/window 的运行时强依赖；保留服务基础设施、前端选择和工具插件装配设置。Telegram 与其他前端通过 ControlAPI 使用 session 配置，Runner 不读取 Telegram 数据。

## Agent 提示与 history 契约

基础使用说明只描述可执行行为：机器选择及 cwd、PTY 游标/截断/超时后继续交互、未知副作用先观察、递归提交的 request_id/waiting_id、queue/steer、skill 查阅与下载、历史查询。不介绍 PostgreSQL、RPC、检查点、SDK 类名或前端投递实现；模型连接、key 和内部 revision 不进入提示。核对现有工具 description，与实际参数和默认行为一致，删除过时、重复说明，不添加额外工具。

向 agent 明确暴露只读关系：

```text
history(
  seq bigint NOT NULL,
  run_id uuid NULL,
  kind text NOT NULL,
  message_id uuid NULL,
  text text NOT NULL,
  data jsonb NOT NULL,
  created_at timestamptz NOT NULL
)
```

该关系仅包含当前 session 的 input/model_request/model_response/final/waiting/error；seq 可有间隙，升序代表历史顺序，data 是该种记录的结构化内容，不承诺它是完整媒体 bytes。SQL 支持 SELECT、过滤、排序、有限 joins/subqueries、count/min/max/lower/length/coalesce；不允许写入、物理表、用户 CTE、任意函数或 JSON 运算符。参数、分页和 substring/fulltext 搜索各提供一条与真实 CLI 一致的示例；需要完整流式回放时使用 session output，不误用过滤后的 history 游标。

这段随当前能力提供的 history 说明进入每轮提示，现有 session 也能看到；保存的用户 instructions 与创建时 skill description 快照保持原值。提取基础行为说明时保持一个来源，避免新旧 session 重复拼接整份基础 prompt。

依据：[OpenAI models](https://developers.openai.com/api/reference/resources/models/methods/list)、[Responses 迁移说明](https://developers.openai.com/api/docs/guides/migrate-to-responses)、[Google models](https://ai.google.dev/api/models)、[Pydantic AI OpenAI](https://pydantic.dev/docs/ai/models/openai/)、[Pydantic AI Google](https://pydantic.dev/docs/ai/models/google/)。
