# Plugin Host

`kapy-plugin-host` 提供独立的 `plugin_host` Python 包，将 RPC 插件声明转换为
Pydantic AI 的静态/动态 instructions 和 14 种普通数据 hooks。Python 3.14；SDK
适配基线为 Pydantic AI 2.40.0。不依赖 Kapy 服务、具体模型 provider 或 Temporal。
插件声明不包含工具，Host 的 `get_toolset()` 使用 SDK 默认的 None。

**公共入口与运行实例**

Agent 定义时直接注册固定 ID 的 Host。选择函数同步读取本次 deps 中可序列化的
manifest 快照；选择函数不执行 I/O，也不依赖 prompt、live capability registry 或
Workflow 内存状态。SDK 在 Activity 恢复时只能从反序列化 deps 重建相同选择。

```python
from pydantic import BaseModel
from pydantic_ai import Agent
from plugin_host import PluginHostCapability, PluginManifest, describe_plugins


class PluginDeps(BaseModel):
    manifests: list[PluginManifest]


host = PluginHostCapability[PluginDeps](
    lambda ctx: ctx.deps.manifests,
    client_factory=client_factory,
    id="selected_plugins",
)
agent = Agent(model, deps_type=PluginDeps, capabilities=[host])
snapshot = await describe_plugins(selected_clients)
result = await agent.run("完成任务", deps=PluginDeps(manifests=snapshot))
```

示例的 `client_factory(plugin_id)` 由调用方提供，只接收插件 ID，返回借入的
`RpcClient`。每次 `for_run()` 校验并复制声明，生成同 ID、同 ordering 的独立运行
Host，重新创建内部组合、导出许可和回调绑定。固定 SDK Agent 绑定随 Host 复制；源
Host 不保存运行 ctx、选择或中间结果。并发运行的 instructions 和 hooks 相互隔离。
未选择事件 hook 时不会强迫模型使用 streaming；空快照产生空 Host。
多个 Host 使用不同且稳定的 ID，缺省 ID 为 `plugin_host`。

静态快照也可使用便捷入口：

```python
from plugin_host import build_plugin_host, load_plugin_host

host = build_plugin_host(snapshot, client_factory=client_factory, id="static_plugins")
# 或发现并保留最初客户端与声明的对应关系：
host = await load_plugin_host(selected_clients, id="static_plugins")
```

- `await describe_plugins(clients)` 按客户端顺序调用 `plugin.describe({})`，解析为
  `list[PluginManifest]`，拒绝重复 plugin ID；结果仅包含声明数据。
- `build_plugin_host(manifests, client_factory=..., id="plugin_host", ordering=None)`
  同步校验、复制声明并完成内部原生排序。构建和 getter 不查找客户端、不执行 RPC。
  构建后修改调用方的列表或嵌套声明不会影响 Host；运行时仍创建独立实例。
- `await load_plugin_host(clients, id="plugin_host", ordering=None)` 组合发现与构建，
  固定最初客户端与声明的对应关系。

`PluginManifest` 支持 Pydantic JSON roundtrip；恢复后的快照可以重复构建和运行，
无需再次 describe。失败不返回部分 Host。三个入口均不拥有客户端或插件进程。

**RPC 与 durable 执行**

`RpcClient.call(method, params)` 是异步接口，须支持并发和取消传播。调用方管理传输
帧、连接、超时和插件进程；Host 不关闭客户端、不创建后台 RPC 任务、不主动重试。

| RPC 方法 | 请求 | 返回 |
| --- | --- | --- |
| `plugin.describe` | `{}` | `PluginManifest` JSON |
| `plugin.invoke` | `{"handler":"导出标识","payload":{...}}` | `InvokeReply` JSON |

hooks 和动态 instructions 共用 Host 固定的
`@durable_operation(name="invoke_plugin")`。客户端仅在该 operation 方法体实际执行时
通过 `client_factory(plugin_id)` 查找；构建、选择和 Workflow 侧数据适配均不解析传输
资源。operation 仅允许本次快照已声明的导出，RPC 永不传递 ctx、deps、Agent、模型、
workspace、数据库连接或本地 callable，也无反向资源访问接口。

普通 Agent 中 operation 直接执行方法体。配置 SDK `TemporalDurability` 后，Workflow
中的 operation 调用由 SDK 转为 Activity；Agent 定义时即以 Host 稳定 ID 注册这一项
通用操作，不会随选定插件或 hook 数量增加注册项。Activity 按 ID 从 Agent 恢复 Host，
调用其 `for_run()` 重建快照后执行同一方法体。

Temporal 调用方在自己的 discovery Activity 中执行 `describe_plugins()`，将结果存入
本次 deps；Workflow 和 Activity 恢复均使用同一快照。调用方配置
`TemporalDurability`、Temporal client 和 `AgentPlugin(agent)`，并注册自己的 discovery
Activity 到 Worker。Host 不实现 Worker 或 engine dispatcher，不需要 Activity 代理
RPC 客户端，也不接入 Kapy。完整的本地示例见
[测试 Workflow](tests/temporal_workflow.py) 与
[真实 Temporal 验收](tests/test_plugin_host_temporal.py)。

Host 不主动重试 invoke；SDK/engine 的 Activity 重试由调用方配置。取消沿 SDK/engine
规则传播，取消本地 await 不保证远端执行被撤销。本地 `PluginHostError` 保留 phase、
plugin_id、handler、point 和原 cause；跨 Activity 使用 SDK/engine 的原生失败表示，
异常对象不保证跨进程保持身份。原生排序和本地工具参数验证保持 SDK 错误类型。

**声明与响应**

```json
{
  "protocol_version": 1,
  "plugin_id": "demo",
  "instructions": [
    {"kind": "text", "text": "检查任务参数。"},
    {"kind": "rpc", "handler": "instructions"}
  ],
  "hooks": [{
    "id": "check",
    "point": "before_tool_validate",
    "handler": "check",
    "ordering": {"position": "outermost", "wraps": [], "wrapped_by": []}
  }]
}
```

所有声明/响应模型拒绝未知字段，包括旧 `tools` 字段；协议版本固定整数 1，布尔值
不合法。plugin ID、hook ID 使用字母开头的字母、数字及下划线。重复插件/hook 身份、
未知排序引用和不支持的 hook 点在声明解析或 Host 组装时失败。

响应是封闭联合：`{"kind":"value","value":...}`、`{"kind":"keep"}` 或
`{"kind":"unhandled"}`。数据必须为 JSON 原生值，拒绝 NaN、Infinity 和任意 Python
对象。动态 instruction 的 payload 为 `{}`，每次模型请求求值，接受 value 中的字符串
或 null；null 表示本次不贡献文本，不额外缓存动态结果。

**Hooks 与排序**

每个 hook 声明对应一个只注册该回调的 SDK 原生 `Hooks`，内部用 `CombinedCapability`
排序和调度。`wraps`/`wrapped_by` 接受同一 Host 中的限定 ID，例如 `demo.check`，允许
跨插件引用。position、稳定同级顺序、循环和冲突使用 SDK 原生规则。

Host 是外部可见的单个 `AbstractCapability`，默认 visitor 不展开内部成员。
构造函数、构建器与加载器的 `ordering=CapabilityOrdering(...)` 约束整个 Host，运行
实例保持这一配置。外部 X → Host → Y、内部 A → B 时，before 为 X → A → B → Y，after
为 Y → B → A → X，错误恢复也使用 SDK 原生逆序链。两层排序不会交错。
内部成员不开启 deferred loading；Host 和内部贡献不提供 spec 序列化。

| hook 点 | payload | value 返回 |
| --- | --- | --- |
| before_run | `{}` | null |
| prepare_tools、prepare_output_tools | `{"tools":[...]}` | 工具列表，只可过滤或修改 description |
| before_model_request | `{"request":{"messages":[...],"model_settings":...}}` | request 数据 |
| after_model_request | request 加 response | ModelResponse 数据 |
| on_model_request_error | request 加 error | 恢复用 ModelResponse |
| before_tool_validate | call、tool、args | 原始字符串或参数对象 |
| after_tool_validate、before_tool_execute | call、tool、args | 参数对象，替换后再次验证 schema |
| after_tool_execute | call、tool、args、result | JSON 结果 |
| on_tool_validate_error | call、tool、args、error | 恢复参数，再次验证 schema |
| on_tool_execute_error | call、tool、args、error | 恢复用 JSON 结果 |
| before_output_validate | `{"output":...}` | 原始字符串或 JSON 对象 |
| on_event | `{"event":...}` | null，仅观察事件 |

工具 hooks 作用于 Agent 自己及其他 Capability 的工具。参数修改与恢复使用有效的
Draft 2020-12 object schema 重新验证，仅允许文档内部引用，验证不会访问网络。
call、tool、response、event 使用对应 SDK Pydantic JSON 表示并包含类型标识；error
仅含 `type_name` 和 `message`。request 替换只修改 messages/model_settings，保留 model
等本地字段。prepare hooks 不允许添加、重命名工具或改写其 schema。

观察 hook 只接受 value(null)。转换 hook 接受 value 或 keep，keep 保留原本地对象。
错误 hook 接受 value 或 unhandled，unhandled 重抛原始异常交给 SDK 恢复链。
wrap hooks、图节点/运行结果替换、流包装和 deferred tool 协调不属于本包接口。

**模块与验证**

`contracts.py` 定义数据模型；`rpc.py` 负责 JSON 边界和借入的 operation 绑定；
`capability.py` 负责发现/构建/运行实例、固定 operation 和内部组合；`hooks.py` 负责 SDK
数据适配与公开注册；`validation.py` 保留工具参数 hooks 所需的本地 schema 校验。

开发依赖包含 SDK Temporal extras，核心依赖不包含 Temporal。测试使用 SDK 确定性模型，
不需要外部模型凭据、PostgreSQL 或 Valkey。完整套件要求真实 Temporal 服务，默认地址
`127.0.0.1:7233`、namespace `default`；可用 `PLUGIN_HOST_TEMPORAL_ADDRESS` 和
`PLUGIN_HOST_TEMPORAL_NAMESPACE` 指定已有服务。用例使用独立 UUID 任务队列和 Workflow
ID，并检查真实 Activity 注册、快照恢复和重放后无新增 RPC；服务不可用时直接失败。

```sh
uv run --package kapy-plugin-host pytest -q -c packages/plugin_host/pyproject.toml packages/plugin_host/tests
# 仅普通 SDK/协议检查，不代表 Temporal 验收：
uv run --package kapy-plugin-host pytest -q -c packages/plugin_host/pyproject.toml packages/plugin_host/tests/test_plugin_host.py
uv run --package kapy-plugin-host ruff check packages/plugin_host
uv run --package kapy-plugin-host ruff format --check packages/plugin_host
uv run --package kapy-plugin-host pyrefly check --config packages/plugin_host/pyproject.toml
uv build --package kapy-plugin-host
```
