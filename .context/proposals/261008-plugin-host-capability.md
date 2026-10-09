# PluginHostCapability 独立包提案

日期：2026-10-08

**1. 目标与范围**

在 `packages/plugin_host/` 提供独立 Python 包，分发名为 `kapy-plugin-host`，导入名为 `plugin_host`。插件仅声明静态 instruction、动态 instruction 和普通数据 hooks；动态逻辑在插件侧执行，Host 通过调用方注入的异步 RPC 客户端访问。

Host 本身负责运行时解析，Agent 定义时直接注册固定 ID 的 `PluginHostCapability`，每次 `.run()` 从 deps 中本次的 manifest 快照生成独立实例。所有 hooks 和动态 instructions 的 RPC 均通过同一个 SDK `durable_operation` 执行；在 Temporal Workflow 中转为 Activity，在普通 Agent 运行中直接执行方法体。

插件不声明工具或 toolset，不提供反向资源访问。RPC 不传递 RunContext、Agent、deps、workspace、model、数据库连接或本地 callable；本地适配函数仍使用 SDK 的 ctx 调度 operation，仅发送显式 JSON 业务输入。普通 hooks 范围见第 6 节；不支持 wrap hooks、图节点替换、流包装或 deferred tool 协调。

独立包不接入 Kapy Runner、Session、HTTP、Telegram 或现有业务插件，也不增加数据库、插件安装或状态管理。调用方拥有传输连接和插件进程，以及 SDK durability capability、Worker 和声明发现 Activity 的配置。

**2. 包结构与依赖**

采用 Python 3.14、uv workspace 和 uv_build。核心依赖为 `pydantic>=2.13.5,<3`、`pydantic-ai-slim>=2.40.0,<3`；参数修改 hooks 仍需 Draft 2020-12 schema 校验，保留 `jsonschema>=4.18,<5`。核心不要求 Temporal extras 或模型 provider，不导入 kapy、shellctl 或具体 engine。SDK 适配基线为 Pydantic AI 2.40.0。

```text
packages/plugin_host/
  pyproject.toml
  README.md
  src/plugin_host/
    __init__.py       # 公共类型与 Host、发现/构建/加载函数
    contracts.py      # instruction/hook 声明、RPC 结果、公共错误
    rpc.py            # 借入客户端、严格 JSON 边界和调用适配
    capability.py     # 稳定 operation、运行时 Host、原生内部组合
    hooks.py          # 普通 hook 数据适配与公开注册
    validation.py     # 工具参数修改 hooks 所需的 schema 校验
```

删除工具声明、RpcToolset 及工具装配代码。根项目保留 workspace 登记，不引入包的业务接入。依赖变化由 uv 生成 uv.lock，不手工编辑生成文件。开发依赖可以包含 SDK Temporal extras，核心包不因此依赖 Temporal。

**3. 公共接口与生命周期**

```python
class RpcClient(Protocol):
    async def call(self, method: str, params: dict[str, JsonValue]) -> JsonValue: ...


class PluginHostCapability(AbstractCapability[DepsT]):
    def __init__(
        self,
        manifests: Callable[[RunContext[DepsT]], Sequence[PluginManifest]],
        *,
        client_factory: Callable[[str], RpcClient],
        id: str = "plugin_host",
        ordering: CapabilityOrdering | None = None,
    ) -> None: ...


async def describe_plugins(clients: Sequence[RpcClient]) -> list[PluginManifest]: ...


def build_plugin_host(
    manifests: Sequence[PluginManifest],
    *,
    client_factory: Callable[[str], RpcClient],
    id: str = "plugin_host",
    ordering: CapabilityOrdering | None = None,
) -> PluginHostCapability[Any]: ...


async def load_plugin_host(
    clients: Sequence[RpcClient],
    *,
    id: str = "plugin_host",
    ordering: CapabilityOrdering | None = None,
) -> PluginHostCapability[Any]: ...
```

构造函数的 manifests 是同步且不做 I/O 的选择函数，通常只读取 `ctx.deps.manifests`。SDK 会在 Activity 内从反序列化 ctx 恢复 Host，因此选择函数必须从可序列化 deps 推导同一份声明，不能依赖 prompt、live capability registry 或 Workflow 内存对象。固定 Host 默认 ID 为 plugin_host，多个 Host 由调用方提供不同稳定 ID。

`for_agent()` 保持稳定 Host，供 SDK 注册操作；`for_run(ctx)` 解析、校验、复制本次声明并生成同一 ID 的独立运行实例。源 Host 不保存本次 ctx、插件选择或中间结果；Activity 恢复仍从相同快照组装实例。原生 hooks 的回调绑定到该运行实例，不共享可变状态。

`describe_plugins()` 按客户端顺序执行 plugin.describe 并返回可序列化 manifest，拒绝重复 plugin_id。`build_plugin_host()` 根据静态快照完成校验、内部排序和本地适配，之后运行依然生成独立实例；构建和 getter 不执行 RPC。`load_plugin_host()` 组合发现和构建，固定最初客户端与声明的对应关系。三个函数均保留便捷用法，不增加兼容旧工具声明的转换。

`client_factory(plugin_id)` 返回借入客户端，仅在真正执行 RPC 的 operation 方法体中调用；Workflow 的组装及数据适配不解析 transport 资源。客户端的连接、超时、并发安全及取消传播由调用方负责，Host 不关闭客户端。

**4. 声明、RPC 与 durable 边界**

PluginManifest 的字段为 protocol_version、plugin_id、instructions、hooks；后两项缺省为空列表。所有模型拒绝未知字段，protocol_version 固定整数 1 并拒绝布尔值。删除 ToolSpec、tools 和工具导出命名规则。

instruction 保持 text/rpc 两种声明：text 为静态字符串，rpc 包含导出 handler。HookSpec 保持 id、point、handler、可选 ordering；排序声明使用原生 position、wraps、wrapped_by，引用格式为 plugin_id.hook_id。身份和 hook 名仍使用字母开头的字母、数字及下划线。重复插件或 hook 身份、未知引用、非法字段及不支持的 hook 点在声明解析/运行实例构建时失败，不返回部分 Host。

| RPC 方法 | 请求 | 返回 |
| --- | --- | --- |
| plugin.describe | {} | PluginManifest 的 JSON 表示 |
| plugin.invoke | handler 与 payload | 带 kind 的 InvokeReply |

InvokeReply 保持 value(JsonValue)、keep、unhandled 封闭联合；所有数据必须为 JSON 原生值并拒绝非有限数值。远端错误由 RpcClient.call 抛异常，Host 补充可确定的插件/handler/point 信息，不增加第二套远端错误码协议。

Host 声明固定名称的 `@durable_operation(name="invoke_plugin")` 异步方法，本地接收 ctx、plugin_id、handler、JSON payload 和可选 point。操作方法体按 plugin_id 获取客户端，执行已声明的 plugin.invoke 并验证结果。仅允许本次快照中声明的导出；实际 SDK 类型转换和 keep/unhandled 应用由 Workflow 侧适配完成。

Agent 构造时 SDK 通过稳定 Host ID 注册这个通用 operation，声明变化不新增 per-plugin 或 per-hook Activity。普通运行中原方法体直接执行；配置 TemporalDurability 的 Workflow 中由 SDK 调度 RPC Activity，Activity 恢复运行 Host 后执行 RPC。调用方通过 AgentPlugin 或 SDK 公共注册接口将这些活动注册到 Worker，不再要求注入自写 Activity 代理客户端。[durable operation 源码](https://github.com/pydantic/pydantic-ai/blob/v2.40.0/pydantic_ai_slim/pydantic_ai/durable_exec/_capability_operation.py)

声明发现仍由调用方的 Activity 执行，快照存入本次 deps。for_run 不重新 describe，避免恢复时声明漂移。包不创建自己的 Temporal Worker，也不导入 temporalio 或自行实现 engine 分派。Host 不贡献工具，不创建 DynamicCapability、DynamicToolset 或插件工具的 get_tools/call_tool/validate_args Activities。

**5. Instruction 适配**

`get_instructions()` 返回静态文本和动态异步回调。getter 本身不做 RPC；动态回调接收本地 RunContext，在每次模型请求求值时调用同一个 invoke_plugin operation，向插件发送空 payload。只接受 value 返回的字符串或 null，null 表示本次不贡献文本；不接受 keep/unhandled，不额外缓存动态结果。

本地贡献和回调只持有固定声明及运行 Host 调用绑定，不保存 ctx。选定插件的 instructions 与 hooks 一起在 for_run 组装；内部 instruction 贡献及普通 Hooks 继续使用 SDK 原生组合，不引入模型协议或新的提示缓存。

**6. Hook 数据接口**

每个 `HookSpec` 生成一个只注册该回调的原生 `Hooks` 实例。注册通过 SDK 公开构造参数或 `on` 命名空间完成；Host 维护 hook 点到公开注册接口的显式映射，不访问私有 registry，也不自行遍历和调度回调。

以下是首版允许的 hook 点与 RPC 数据契约。`call` 和 `tool` 分别是 `ToolCallPart` 与 `ToolDefinition` 的数据表示；`request` 只包含 `messages` 和 `model_settings`；`error` 只包含错误类型名称和 message，不包含异常对象或 traceback。

| Hook 点 | payload | `value` 的含义 |
| --- | --- | --- |
| `before_run` | `{}` | 必须为 null，作为通知 |
| `prepare_tools`、`prepare_output_tools` | `{"tools": [...]}` | 过滤后的工具列表，可修改 description |
| `before_model_request` | `{"request": {...}}` | 替换 messages 和 model_settings 的数据 |
| `after_model_request` | `{"request": {...}, "response": ...}` | 替换模型响应 |
| `on_model_request_error` | `{"request": {...}, "error": ...}` | 恢复用模型响应 |
| `before_tool_validate` | `{"call": ..., "tool": ..., "args": ...}` | 替换原始参数，类型为字符串或 JSON 对象 |
| `after_tool_validate`、`before_tool_execute` | 同上 | 替换已验证参数，类型为 JSON 对象 |
| `after_tool_execute` | 上述字段加 `result` | 替换 JSON 工具结果 |
| `on_tool_validate_error`、`on_tool_execute_error` | 对应工具输入加 `error` | 分别返回恢复用参数或工具结果 |
| `before_output_validate` | `{"output": ...}` | 替换原始字符串或 JSON 对象 |
| `on_event` | `{"event": ...}` | 必须为 null，仅观察事件 |

观察 hook 只接受 `value(null)`；转换 hook 接受 `keep` 或符合该点类型的 `value`；错误 hook 接受 `unhandled` 或符合恢复类型的 `value`。错误 hook 返回 `unhandled` 时，本地适配函数抛出原始异常，由原生恢复链继续处理。

SDK 数据模型使用对应的本地 Pydantic 类型进行 JSON 转换和恢复。`before_model_request` 保留原有 request 对象中的 model 等本地字段，仅应用允许替换的数据；普通转换 hook 返回 `keep` 时保留原对象。任意工具参数和结果仅支持 JSON 原生值，不能无损表示的对象在 RPC 前明确拒绝，不使用 pickle、repr 或通用对象句柄。

`prepare_tools` 类回调只能过滤已有工具或修改 description，不能添加可执行工具、重命名或改写参数 schema。参数在 SDK 校验后被 hook 替换时重新按该工具的有效 JSON Schema 校验；校验错误 hook 的恢复参数也须经过同样的参数校验。工具 hook 根据收到的工具名称处理输入，需要保持输入时返回 `keep`。

模型响应的替换与恢复值须解析为 `ModelResponse`。不认识的 hook 点、图节点/运行结果替换以及需要 callable 或异步流的接口不会被自动降级成普通通知。

**7. Host 整体排序与内部原生组合**

Host 内部持有 CombinedCapability，将 instruction getter 和支持的 hook 方法委托给内部组合；get_toolset 保持默认 None。默认 apply/visit_and_replace 只暴露外部 Host，内部 RPC Hooks 不与 Agent 的其他 Capability 交错。

先为各 hook 创建原生 CapabilityOrdering 和 Hooks，再将限定 ID 转为实例引用，最后让 CombinedCapability 排序。仅允许同一 Host 内引用；拓扑排序、位置层级、循环冲突和同级稳定顺序全部使用 SDK，不引入新 priority 或排序表。before 按链正序执行，after/错误恢复按 SDK 逆序链。

外部 ordering 约束整个 Host，必须在原 Host 及 for_run 返回实例上保持。内部成员不启用 deferred loading；Host 和借入调用绑定的贡献不提供 spec 序列化。has_on_event/listens_to 委托当前运行的内部组合，未选择事件 hook 时不强迫流式模型运行。

**8. 资源与错误语义**

Host 保存声明快照和本地适配，不拥有客户端连接或插件进程，不创建后台 RPC 任务。每次运行的组合相互隔离，且不保存 ctx。动态 instructions 和 hooks 均在当前调用中 await；RPC 客户端仅发送 JSON 业务输入。

Host 自身不自动重试 RPC；durable engine 的 Activity 重试由调用方的 SDK 配置决定。取消继续按 SDK/engine 语义传播，停止本地等待不表示远端逻辑已撤销。

本地 PluginHostError 保留 phase、plugin_id、handler、point 和原 cause。跨 Activity 的失败与取消使用 SDK 和 engine 的原生传递语义，不声称异常对象跨进程保持身份。内部原生排序及本地工具参数校验仍保留各自 SDK 错误类型。任何声明或组装失败不返回部分 Host。

**9. 实现变更**

改动限于独立包的声明、RPC operation、Host 生命周期、数据适配、公开导出、测试和文档。删除插件工具声明/执行及 DynamicCapability 使用示例；保留 Agent 自己及其他 Capability 的工具 hooks。工具参数修改/恢复所需校验从工具模块移到独立 validation 模块。

SDK 原生 Hooks/CombinedCapability 排序、工具参数恢复、事件选择、严格 JSON、错误与取消边界继续适用。依赖变更由 uv 生成锁文件，业务 Runner、接口、数据库及 vendored shellctl 不改动。
