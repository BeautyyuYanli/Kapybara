# Provider 管理、session 模型选择与 agent 使用说明

以 `kapy_v2.md` 和用户最新补充为准。参考 Pydantic AI 将连接 provider 与 model 分离：provider 管调用协议、端点、key；session 选择 provider、模型名称及窗口。保留控制面/执行面、State session、插件工具及前端接口的职责。

## Provider API 与生命周期

新增独立的 `provider.create/get/list/update/delete，以及下述模型目录 API` 控制 API，全部经过既有 ControlAPI；不把 provider 凭据塞进 session。

```python
type ModelType = Literal["openai_responses", "openai_chat", "google_ai_studio"]

class ProviderConfig(BaseModel):
    name: str
    type: ModelType = "openai_responses"
    base_url: str | None = None
    api_key: SecretStr | None = None
```

```text
provider.create({request_id, name, type?, base_url?, api_key}) -> ProviderView
provider.get({provider_id}) -> ProviderView
provider.list({after_id?, limit?}) -> {items, next_after_id}
provider.update({provider_id, request_id, expected_revision,
                 name, type, base_url, api_key?}) -> ProviderView
provider.delete({provider_id, request_id, expected_revision}) -> {deleted: true}
ProviderView = {id, name, type, base_url, has_api_key, revision, created_at, updated_at}
```

provider 默认协议是 openai_responses，也可显式选择 openai_chat 或 google_ai_studio；不再增加独立 kind 字段。省略地址时采用对应协议的标准地址，保存解析后的实际值。OpenAI 地址是含 `/v1` 的 API base；Google 地址是 Gemini API host/base，由 SDK 添加版本与资源路径。允许自定义地址，拒绝 URL 内嵌凭据、query 和 fragment。创建显式提供 key；更新时省略 key 保留原值，但修改 type/base_url 必须显式提供 key，不能自动把旧 key 发往新地址。不从 SDK 环境变量兜底。

本系统按原设计是单部署管理员：operator 和已认证、受信前端可以管理和使用 provider。session capability 不拥有 provider 管理权限，只能读取自己当前已绑定 provider 的非秘密信息和模型列表，不能枚举其他连接或选用未知 provider；递归子 session 可以继承已认证调用者的绑定。provider UUID 本身不授予权限。

Gateway 自有单表 `gateway_providers` 保存当前连接、私有 key、revision 和 deleted 标记。create/update/delete 与既有请求幂等 receipt 在同一数据库事务完成。摘要包含完整输入，持久通用 params/result/error 剥离 key；相同 request_id 换参数（包括 key）报冲突。update/delete 使用 expected_revision 防止覆盖并发修改，不引入 provider 版本历史或凭据转移服务。

每次 Runner 调用（包括控制进程恢复）读取当前 provider，将配置冻结为该次运行的对象。provider 更新不热改正在运行的对象，下一次调用使用新配置。delete 标记删除并清除 key，不删除 session 历史；已有运行可结束，后续调用明确报告 provider 不可用，session.update 可以重新选择 provider。不保留已被替换或删除的旧 key。session 删除不影响其他 session 共用的 provider。

普通 provider/session 查询、State 历史、模型提示、错误和日志均无 key。参数以 SecretStr 校验，验证错误不回显原始输入。移除未完成的 session secret revision 方案，不增加 State 与 provider 的跨包 FK 或额外绑定表。

## Provider 模型目录与默认配置

provider 持久保存探测到的模型，每个模型拥有稳定 UUID。Gateway 自有 `gateway_provider_models` 表保存 id、provider_id、name、discovered JSONB、defaults JSONB、revision、discovered_at/created_at/updated_at，UNIQUE(provider_id,name)。name 是供应商实际模型名，不可通过 update 改名；同一 provider/name 重复探测始终更新同一行，保留 id。

```text
provider.discover({provider_id, request_id, page_token?, limit?}) ->
  {items: ModelView[], next_page_token: str|null}
provider.models({provider_id, after_id?, limit?}) ->
  {items: ModelView[], next_after_id: UUID|null, default_model_id: UUID|null}
provider.model.create({provider_id, request_id, name, defaults?}) -> ModelView
provider.model.get({model_id}) -> ModelView
provider.model.update({model_id, request_id, expected_revision, defaults}) -> ModelView
ModelView = {id, provider_id, name, discovered, defaults, revision,
             discovered_at, created_at, updated_at}
ModelDefaults = {context_window_tokens?: int|null, max_output_tokens?: int|null}
```

provider.models 只读本地目录，default_model_id 仅在该 provider 的完整目录只有一个模型时返回，否则为 null。provider.discover 是显式网络动作：OpenAI 请求 models，Google 使用原生 models list/get 并筛选 generateContent。每个调用只处理一个有界页面，返回供应商 next_page_token；结果与 request receipt 一次提交。provider 在网络调用期间改变时拒绝提交过时观察值。同 UUID 重试返回第一次已确认结果；刷新不覆盖 defaults，不因为某一页缺少某个模型就删除旧行或假称它已不可用。

只保存有界的模型描述和预算 metadata，discovered 表达观察到的信息，不代表用户配置。provider 改协议或地址时清除旧端点的 discovered 值/时间，保留模型 ID 和用户 defaults；同名模型在新端点是否可用需要重新探测或由用户确认。不保存探测历史、不建立同步任务或失效调度框架。

provider.model.create 支持手工登记模型，不依赖端点开放 models。相同 provider/name 已存在则明确冲突，调用者用 get/update 修改默认值。model.update 的 defaults 是完整替换，缺省或 null 表示移除手工覆盖；revision CAS 防止并发覆盖。model mutations 与 receipt 复用 Gateway 同一数据库事务。目录读取权限沿 provider；只有 operator/可信前端可探测和管理目录，session capability 只能读当前已绑定 provider 的目录。

## Session 模型选择

session.create/update 的 config.model 只保存 model_id 与可选预算覆盖；config.instructions 保持字符串，其他 session 字段和 UUID 幂等语义不变。

```python
class SessionModelConfig(BaseModel):
    model_id: UUID
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
```

provider 由 model_id 解析，调用协议完全来自 provider；session 不接收端点、key、协议或任意原始模型名。session 身份递归创建时默认继承当前调用者的 model_id 和显式覆盖，可选择同一已授权 provider 的其他已登记模型；不能仅凭一个 UUID 使用另一连接。更换 model_id 时未明确提供的预算重新继承，不带上另一模型的覆盖。前端/operator 从目录选择稳定模型 ID；只有唯一候选时可以采用 provider.models 返回的 default_model_id。

预算取值顺序为 session 显式覆盖 → 模型 defaults → 模型 discovered metadata → 固定兜底。context window 兜底 256 * 1024 = 262144，max output 兜底 16 * 1024 = 16384。OpenAI metadata 没有窗口则保留未知，Google 对应 inputTokenLimit/outputTokenLimit；不从模型名猜测。最终输出预算须为正且小于窗口，并遵守已知供应商硬上限，冲突明确报配置错误。固定默认值是用户指定策略；实际用量仍只读 API usage。

State 保存 model_id 和用户显式覆盖，不把派生值写成永久 session 覆盖。Gateway 的一个解析入口负责授权、继承和计算有效设置；创建/更新时验证选择，每次 Runner 调用从当前目录计算并冻结该次配置，期间不重新探测或热刷新。provider/defaults 更新影响下一次运行（包括重启恢复），不影响已开始的实例。session.update 仍要求 waiting，普通查询返回保存的选择和覆盖；完整模型默认值通过 provider.model.get 查询。

旧 session 保留历史，可经 session.update 指定已登记 model_id 后继续；不持续读取旧部署模型变量兜底。session 删除不删除共享目录或 provider。

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

投影持久保存 provider_id/revision、协议、端点、模型 ID/名称和有效预算。连接或模型身份变化时，只在待发送投影中清除原供应商专有 ID、签名与不透明推理项，保留用户/assistant 正文、媒体和成对工具记录；原始历史不变。恢复若读到已更新 provider，同样处理。模型身份或有效预算改变时清掉旧 usage 的压缩触发依据；仅调整窗口不必删除供应商签名。无需转换注册框架或旧 key 保留机制。

三种协议的实际 usage 归一到现有计数，继续仅用 provider API 最新单次响应，不添加本地 tokenizer。context/media 错误分类使用真实 SDK 字段；鉴权、限流等其他错误安全地交 State，不伪装媒体拒绝。工具集合保持既定进程工具、read_media、wait 和注入 ScriptTool；apply_patch 走普通插件入口。

依赖使用 pydantic-ai-slim[google,openai]。当前 google-genai 要求 websockets<17，因此采用16.x，uv 生成 lock。Settings、Compose 和配置示例移除模型连接及窗口的运行时强依赖，保留基础设施、前端选择和工具装配设置。

## CLI 与 Telegram

CLI 新增 provider 子命令对应连接 CRUD、discover/models 和 model create/get/update。provider create/update 接受 JSON 配置文件和 key-file/key-env；key 放请求，不打印。session create/update 接受模型 JSON 配置文件，--model 使用稳定模型 ID，不保留名称/ID猜测双语义；递归创建默认继承当前 session。默认值通过 provider model update 配置，不在 CLI 内另写预算解析算法。

Telegram /providers 列出连接，/provider ID 选择已有连接；/provider JSON 创建并选择，JSON 带 provider_id/expected_revision 时更新。/discover 显式探测当前 provider，/models 读取持久目录。/model ID 或 /model JSON 保存模型 ID 与预算覆盖；/modeldefaults JSON 更新所选模型的默认值，使用已读取 revision。只有目录唯一候选时自动选择 default_model_id，否则提示选 ID。/new 使用 chat/topic 保存设置。

更新 provider 或模型 defaults 会影响引用它的后续运行，命令回执明确该行为；忙碌 session 的选择/覆盖保存供下一 /new，不热改运行。配置命令不作为普通 prompt；/settings、回执和错误隐藏 key。待处理 provider 配置属于 Telegram 私有 ingress；已保存设置仅含 provider_id、model_id 和非秘密覆盖，key 不进入 delivery projection 或 session 历史。Telegram 和其他前端经 ControlAPI 操作 provider/session，Runner 不读取前端表。

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
