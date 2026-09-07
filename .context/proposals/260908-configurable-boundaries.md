# Kapy v2：可配置边界与 agent 使用契约

基于 `ca22ab7`，修正对齐审查确认的模型、Telegram 耦合和额外文本工具造成的设计偏离。保留 State、Execution、Skills 的持久状态与 RPC，不扩展分布式调度或原生 Anthropic 等协议。用户本轮授权由主代理直接推进，不再通过 senior 层转派。

## 模型适配与配置

`kapy.agent.models` 导出 `ModelBackend` 协议：`create_model(model_name: str) -> pydantic_ai.models.Model` 和 `classify_error(error: Exception) -> ModelFailure | None`。`ModelFailure` 是不可变的 `{kind: Literal['context_length','media'], message: str}`，message 必须可安全写入历史。`OpenAICompatibleBackend(*, base_url: str, api_key: SecretStr, http_client: httpx2.AsyncClient)` 是首个实现：显式构造 OpenAI Chat Completions 适配器，负责现有错误分类与凭据、URL query、大块数据的脱敏。未知错误不转成媒体拒绝或上下文超限。

Runner 直接依赖该协议和 Pydantic AI 的通用消息、Model、usage；不再构造供应商 client 或读取其 key。`RunnerConfig` 只保留 `model`、窗口、输出、压缩和媒体限额；`Runner(config, machine_caller, *, model_backend: ModelBackend, payload_store, authorize_wait, plugins=())` 接收适配器。每轮从 session.config.model 选择模型名，窗口使用部署显式配置。媒体修复、两次有界上下文重试、fresh usage 一次 sweep、checkpoint/原始历史语义保持不变。

Settings 使用 `model_base_url`、`model_api_key`、`model`，环境名为 `KAPY_MODEL_BASE_URL`、`KAPY_MODEL_API_KEY`、`KAPY_MODEL`。现有 `OPENAI_BASE_URL/OPENAI_API_KEY/OPENAI_MODEL` 仅作为环境迁移别名；中性变量优先，不保留第二套 Python 字段。默认值沿用现有部署，模型名不做 OpenAI 型号白名单。Compose 和 `.env.example` 使用中性配置并支持现有 `.env`，不修改真实凭据文件。

`create_app(settings=None, *, model_backend: ModelBackend | None = None, frontend_factories: Mapping[str, FrontendFactory] | None = None, plugins: Sequence[ScriptTool] | None = None)` 作为装配入口。未注入 backend 时由 Gateway 创建并借出 HTTP client；注入时不要求配置模型 API key，不关闭借用的外部资源。窗口配置仍必需。

## 前端端口与所有权

将 `Frontend`、`FrontendFactory`、`FrontendContext` 和 `ControlAPI` 放入独立的 Gateway 前端接口模块。`ControlAPI.call(method: str, params: JsonObject, *, principal: Principal) -> Awaitable[JsonValue]` 是前端 session/input/output/history 等操作的唯一端口；Frontend 继续以 `async run() -> None` 管理自己的迁移、资源与后台任务。Context 提供配置、该端口、借用的 metadata pool 和 schema，不暴露具体 State/Runner/ControlService。

Settings.frontends 是可选名称列表，配置为 `KAPY_FRONTENDS` JSON 数组；显式 `[]` 关闭所有内置前端。未指定时按现有 token 自动启用 Telegram。默认工厂表只注册 Telegram；应用可以通过 `frontend_factories` 注册其他可信 Python 工厂，再由名称列表选择。未知名称在启动资源前报错。只校验实际启用的 Telegram 配置，禁用时不因残留 bot 配置阻止纯控制服务启动。不从用户 RPC 或模型参数导入 Python 路径。

Principal.kind 改为 operator/session/frontend；frontend 使用经过验证的 `frontend_id` 与不透明 `subject`，持久 ID 为 `frontend_id:subject`。operator/session 命名空间保留。Telegram subject 继续使用 bot/chat/thread 三元组，因此历史 `telegram:bot:chat:thread` principal、owner、request、channel grants 不需重写。核心统一按 frontend owner 授权，恢复用通用持久 ID 解码，不再解析 Telegram route。

Telegram 自己创建现有四张 Telegram 表，名称、列、projection、cursor、pending 和 raw offset 不变。核心 migration、begin_cleanup、mark_deleted 不引用这些表。Telegram 的输入解析和投递通过 ControlAPI 检查 session；删除中或已删除时由插件清理自己的 delivery 和 route 绑定。正式 pending 发送前也检查状态，插件停用期间的陈旧绑定在下次启用时清理。核心 session 删除、receipt 和机器/payload outbox 不依赖前端是否在线，也不增加前端回调队列。保留原生 Rich Markdown、稳定草稿、重试和 ACK 接续。

Telegram 将 create 回执中的 session_id 保存在既有 inbox.resolved_action，用于同 route 的回复排序，不在正常投递中 JOIN 核心 request 表。旧已处理创建记录仅在相关 delivery 存在且缺少该字段时，使用保存的原始参数、主体和 UUID，通过同一幂等回执补齐；不重建请求、不生成新 UUID、不重放无关历史。

## agent 指令与工具

新 session 的基础指令说明在选定机器工作，通过 `kapy control session create` 取得 submission.waiting_id、再用 wait 接续子任务，steer/queue 与默认输入监听，以及通过 skill list/read/download/upload 和 history read/search/query 获取资源。CLI 示例使用真实参数顺序，指明 caller 身份自动继承、目标 `--session` 不改变身份，不让模型填写 token。说明任务行为，不介绍数据库、RPC、checkpoint 或供应商实现。

每轮在保存的 instruction/skill snapshot 之外追加当前 session ID、关联机器 ID 和默认机器的简短环境说明；关联不等于在线，不进行隐式远端探测。旧 session 的保存指令与 skill snapshot 不覆盖，新基础说明用于新 session。

PTY/stdio、状态观察、next byte cursor、展示截断与底层完整输出、换行和 Ctrl-C 等细节集中在工具 description，基础指令不重复工具手册。参数补全任务相关 description。process_wait/write 的 cursor 用明确的 PTY 或 stdout/stderr Pydantic 联合类型代替任意整数 dict，沿用相同 JSON wire shape；支持省略游标从头观察。write 明确仅适用于 PTY，kill 改为真实的 process group/best-effort 描述。插件统一附加的 machine_id 也提供默认机器语义。wait 仍只有 wait_for 一个数组参数。apply_patch 的生成描述不手工修改。

## apply_patch 作为注入插件

Runner 仅运行传入的 ScriptTool，不自动添加 apply_patch。ScriptTool 保留参数模型和 render，增加一个可选 `prepare: Callable[[ScriptHost, ProcessCommand], Awaitable[ProcessCommand]] | None`。所有脚本工具统一使用 render → 可选 prepare → 标准进程执行，不按工具名字分派。

ScriptHost 仅包装本次 Operation 的 workspace、stdio run、push 能力，不暴露 Runtime、ControlService 或凭据。稳定子操作 ID 仍由 Operation 生成并持久化；准备命令确认终态成功才继续，未知结果不重发。

`apply_patch_plugin()` 的准备逻辑放在独立插件模块：读取并验证生成的 manifest/平台资源，通过普通命令检查目标 hash、必要时复用字节传输安装并 chmod，然后返回绑定实际可执行文件的 ProcessCommand，补丁仍走 stdin。MachineTools 不内置该插件的下载、安装或名称判断。ScriptHost 与 apply_patch_plugin 显式导出。

Settings.tool_plugins 由 `KAPY_TOOL_PLUGINS` JSON 数组配置，默认 `["apply_patch"]`，`[]` 关闭。Gateway 在 `plugins is None` 时按已知本地插件名装配默认插件；显式 `plugins=()` 关闭，传入自定义 ScriptTool 序列则使用该序列。未知配置名在启动资源前拒绝，不动态加载用户提供的 Python 路径。默认提供 apply_patch 是应用装配选择，不是 Runner 的内置工具契约。

## 模型工具边界

删除额外暴露给模型的 file_read/file_write 工具、专属参数和实现。模型工具范围回归进程管理、read_media、脚本插件和 wait。文本查看使用进程命令，文件修改使用命令或 apply_patch，不新增文本分页或 UTF-8 解码层。

Execution file.push/pull/chunk/finish/abort 保持原始字节传输；Operation.push/pull 继续供媒体、脚本 stdin 和插件安装使用，Skills 传输不变。已完成历史保留；恢复未完成且已移除的工具调用时，使用通用未知工具的可纠正结果，不重放未知写副作用，也不保留旧工具兼容入口。删除只为额外文本工具存在而编写的测试，保留底层原始字节、媒体、脚本及恢复的行为验证。

同步修改受影响调用点、测试和当前架构/模块文档，删除仍描述初始未实现方案的过时措辞；历史 proposal 和验收记录保持历史事实。
