# Provider 管理、session 模型选择与 agent 使用说明

以 `kapy_v2.md` 和用户最新补充为准。参考 Pydantic AI 将连接 provider 与 model 分离：provider 管供应商、端点、key；session 选择 provider、调用协议、模型名称及窗口。保留控制面/执行面、State session、插件工具及前端接口的职责。

## Provider API 与生命周期

新增独立的 `provider.create/get/list/update/delete/models` 控制 API，全部经过既有 ControlAPI；不把 provider 凭据塞进 session。

```python
type ProviderKind = Literal["openai", "google_ai_studio"]

class ProviderConfig(BaseModel):
    name: str
    kind: ProviderKind = "openai"
    base_url: str | None = None
    api_key: SecretStr | None = None
```

```text
provider.create({request_id, name, kind?, base_url?, api_key}) -> ProviderView
provider.get({provider_id}) -> ProviderView
provider.list({after_id?, limit?}) -> {items, next_after_id}
provider.update({provider_id, request_id, expected_revision,
                 name, kind, base_url, api_key?}) -> ProviderView
provider.delete({provider_id, request_id, expected_revision}) -> {deleted: true}
provider.models({provider_id, page_token?, limit?}) ->
  {items: [{name, context_window_tokens: int|null, max_output_tokens: int|null}],
   next_page_token: str|null}
ProviderView = {id, name, kind, base_url, has_api_key, revision, created_at, updated_at}
```

省略地址时采用供应商标准地址，保存解析后的实际值。OpenAI 地址是含 `/v1` 的 API base，可同时服务 Chat 和 Responses；Google 地址是 Gemini API host/base，由 SDK 添加版本与资源路径。允许自定义地址，拒绝 URL 内嵌凭据、query 和 fragment。创建显式提供 key；更新时省略 key 保留原值，但修改 kind/base_url 必须显式提供 key，不能自动把旧 key 发往新地址。不从 SDK 环境变量兜底。

本系统按原设计是单部署管理员：operator 和已认证、受信前端可以管理和使用 provider。session capability 不拥有 provider 管理权限，只能读取自己当前已绑定 provider 的非秘密信息和模型列表，不能枚举其他连接或选用未知 provider；递归子 session 可以继承已认证调用者的绑定。provider UUID 本身不授予权限。

Gateway 自有单表 `gateway_providers` 保存当前连接、私有 key、revision 和 deleted 标记。create/update/delete 与既有请求幂等 receipt 在同一数据库事务完成。摘要包含完整输入，持久通用 params/result/error 剥离 key；相同 request_id 换参数（包括 key）报冲突。update/delete 使用 expected_revision 防止覆盖并发修改，不引入 provider 版本历史或凭据转移服务。

每次 Runner 调用（包括控制进程恢复）读取当前 provider，将配置冻结为该次运行的对象。provider 更新不热改正在运行的对象，下一次调用使用新配置。delete 标记删除并清除 key，不删除 session 历史；已有运行可结束，后续调用明确报告 provider 不可用，session.update 可以重新选择 provider。不保留已被替换或删除的旧 key。session 删除不影响其他 session 共用的 provider。

普通 provider/session 查询、State 历史、模型提示、错误和日志均无 key。参数以 SecretStr 校验，验证错误不回显原始输入。移除未完成的 session secret revision 方案，不增加 State 与 provider 的跨包 FK 或额外绑定表。

## Session 模型配置与发现

`session.create/update` 的 `config.model` 为非秘密结构化对象，`config.instructions` 保持字符串；其他 session 字段与 UUID 幂等语义不变。

```python
type ModelType = Literal["openai_responses", "openai_chat", "google_ai_studio"]

class SessionModelConfig(BaseModel):
    provider_id: UUID
    type: ModelType | None = None
    name: str | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int = 16_384
```

OpenAI provider 的默认 type 是 openai_responses，可显式选择 openai_chat；Google provider 对应 google_ai_studio，不走 OpenAI-compatible 转译。类型必须与供应商匹配，session 不能覆盖连接地址/key。

session 身份创建子 session 时默认继承调用者的模型配置，显式字段覆盖继承值；前端与 operator 提交选择，没有服务端默认连接。更新 session 仍要求 waiting，公开配置按完整替换语义处理。更换模型名而未明确提供 window 时重新解析，不沿用另一模型的窗口。无完整模型配置的旧 session 保留历史，可经 session.update 配置后继续；不持续读取旧部署模型变量作为兜底。

规范化、继承和发现由 Gateway 同一个解析入口完成；前端调用 provider.models 展示候选，Runner 只接收已解析的配置。OpenAI 查询 `/models`，Google 使用 models list/get，并筛选支持 generateContent 的模型。分页、响应体和超时有界；名称缺失时，只有唯一候选且没有未读页才自动选择，否则明确要求指定模型名。名称和窗口均明确时不强制探测，兼容不开放 models 接口的端点。

窗口只取用户显式值或供应商返回值。OpenAI 标准 models 不提供窗口，不从模型名猜测；Google 使用 inputTokenLimit，已获知的 outputTokenLimit 约束输出配置。显式窗口优先，输出预算必须为正且小于窗口。未知窗口明确报参数错误。首次已解析的 session 写入结果保存到既有 request.operation，恢复同 UUID 不因目录变化重新选择模型；provider 更新、删除等操作使用自身明确的生命周期。

## 模型适配与运行

保留 ModelBackend 的 create_model(name)/classify_error(error) 借用接口，连接配置为内部类型，与 SessionModelConfig 分开。

```python
@dataclass(frozen=True)
class ModelConnection:
    type: ModelType
    base_url: str
    api_key: SecretStr

type ModelBackendFactory = Callable[[ModelConnection, httpx2.AsyncClient], ModelBackend]

def create_model_backend(
    connection: ModelConnection, http_client: httpx2.AsyncClient,
) -> ModelBackend: ...
```

工厂构造 Pydantic AI 的 OpenAIResponsesModel/OpenAIChatModel/GoogleModel，显式注入连接和借用 HTTP client。create_app(..., model_backend_factory=...) 支持替换适配器。Gateway 每次运行读取 provider 并创建独立 Runner；RunnerConfig 的模型名、窗口和输出预算来自 session，不共享可变配置。Runner.initial_state 改为无实例依赖的静态入口，继续保存用户 instructions 和创建时 skill catalog。

Responses 使用 store=False、完整本地历史，不启用 previous_response_id/conversation 或供应商自动截断。禁用依赖原始 Responses item ID 的历史回放，保留需要的 reasoning 加密内容与工具调用配对。Google 保留 thought signature 及对应工具协议。同一连接恢复不得重做已完成工具。

投影持久保存 provider_id/revision、协议、端点及模型身份。身份变化时，只在待发送投影中清除原供应商专有 ID、签名与不透明推理项，保留用户/assistant 正文、媒体和成对工具记录；原始历史不变。恢复若读到已更新 provider，同样按该规则处理；清掉旧 usage 的压缩触发依据，不把旧模型计数当成新窗口输入。无需增加转换注册框架或旧 key 保留机制。

三种协议的实际 usage 归一到现有计数，继续仅用 provider API 最新单次响应，不添加本地 tokenizer。context/media 错误分类使用真实 SDK 字段；鉴权、限流等其他错误安全地交 State，不伪装媒体拒绝。工具集合保持既定进程工具、read_media、wait 和注入 ScriptTool；apply_patch 走普通插件入口。

依赖使用 pydantic-ai-slim[google,openai]。当前 google-genai 要求 websockets<17，因此采用16.x，uv 生成 lock。Settings、Compose 和配置示例移除模型连接及窗口的运行时强依赖，保留基础设施、前端选择和工具装配设置。

## CLI 与 Telegram

CLI 新增 provider 子命令对应六个 API，create/update 接受 JSON 配置文件和 key-file/key-env；key 放请求，不打印。session create/update 使用模型 JSON 配置文件，保留 --model 名称快捷参数，不为每个字段重复增加旗标；递归创建默认继承当前 session。

Telegram 新增 /providers 列出连接，/provider ID 选择已有连接；/provider JSON 创建连接并选择它，JSON 带 provider_id/expected_revision 时更新该连接。/model JSON 或 /model NAME 保存模型选择、协议和窗口；/models 调 provider.models。/new 使用 chat/topic 保存设置。修改 provider 资源会影响该连接后续运行，命令回执明确这一点；忙碌 session 的模型设置保存供下一 /new，已有运行不热改。

配置命令不作为普通 prompt 输入；/settings、回执和错误隐藏 key。待处理 provider 配置命令属于 Telegram 私有 ingress，完成后保存设置只需 provider_id 和非秘密模型字段；key 不进入 delivery projection 或 session 历史。Telegram 和其他前端经 ControlAPI 操作 provider/session，Runner 不读取前端表。

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
