# Capability 插件

`kapy.capability_plugins` 是独立的 Pydantic AI 插件模块。使用端静态注册配置类型和
同步工厂，在 Agent 构造时安装一个 `DynamicCapability`，每次 `agent.run` 通过
deps 决定启用哪些插件及其完整配置。模块不依赖旧插件、控制面、数据库或 runner，
也不自动注册插件。

```python
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from kapy.capability_plugins import PluginDeps, PluginRegistry, PluginSpec
from kapy.capability_plugins.examples.echo import EchoConfig, build_echo

registry = PluginRegistry[PluginDeps]()
registry.register("echo", config_type=EchoConfig, factory=build_echo)
agent = Agent(TestModel(), deps_type=PluginDeps, capabilities=[registry.capability()])

result = await agent.run(
    "回显文本",
    deps=PluginDeps(plugins=(PluginSpec(plugin_name="echo", config={"prefix": "A:"}),)),
)
result = await agent.run("普通回答", deps=PluginDeps())  # 关闭全部插件。
```

## 配置与组合

`PluginSpec` 只有 `plugin_name` 和 JSON `config`，缺省配置为 `{}`，也允许 JSON null
供 `RootModel` 等模型使用。`PluginDeps.plugins` 是完整的启用集合；顺序决定默认
组合顺序，同一插件在一次运行中只出现一次。模块不合并默认、增量或 session 配置。
使用端可继承 `PluginDeps` 增加业务字段，并将实际子类用于 `PluginRegistry` 和
Agent 的 `deps_type`。

Registry 内的名称非空且唯一，注册错误抛出 `ValueError`。`capability(id="plugins")`
捕获当前注册项的副本；后续注册不改变已创建的入口。每次解析先检查选择，再深拷贝
配置、通过注册的 Pydantic 模型校验，最后调用对应工厂。工厂须创建新的
`AbstractCapability`，也可返回包含多个子项的 `CombinedCapability`。未知名称、重复
选择、配置校验失败和错误返回类型抛出 SDK `UserError`；工厂本身的执行异常直接传播。

插件配置是只读输入，DTO 的 `frozen` 只限制字段赋值，不递归冻结 JSON。每次解析的
配置对象和 capability 独立，Registry 不保存这些对象或运行状态。插件为有身份语义
的 capability 提供稳定 ID；Temporal 中带工具的 capability 必须声明 ID。插件直接声明工具名，
模块不重命名工具，冲突交给 SDK。

普通聚合使用 SDK `CombinedCapability`，空集合返回 `None`。Instructions、hooks、model
settings、native tools 和 toolsets 使用原生契约。SDK 自动调用子项的 `for_agent` 和
`for_run`，插件可按 `for_run` 契约隔离可变状态。沿用 SDK 的排序和 middleware
语义，不增加 priority 或依赖解析器。插件组与宿主静态 capability 作为整体组合，
排序声明不能假定组内插件与宿主可以任意交错。

项目锁定的 SDK 2.40.0 给 `CombinedCapability` 的子工具包装时会读取 live
`ctx.capabilities`，而 Temporal 的 Activity 上下文不携带这些实例。模块仅在
`TemporalRunContext` 的 Activity 重建路径替换该工具归属包装：保留已有
`capability_id`，缺失时使用插件声明的稳定 ID，并合并 `defer_loading`；其余
`ToolDefinition` 字段，包括 metadata、审批要求、strict 和 sequential，保持不变。
普通运行和 Workflow 侧的工厂返回原生 `CombinedCapability`，保留普通运行中 SDK
延迟加载器对 Toolset instructions 的收集；其余组合和资源生命周期由 SDK 负责。
模块不序列化 capability 对象，也不构造临时 context registry。

SDK 2.40.0 的 `DynamicCapability` 外层工具集不向嵌套工具集转发逐步解析，因此
插件内部的 `DynamicToolset` 或 `Capability(toolsets=[callable])` 不会自动逐步重建。
本模块实现的是每次 `agent.run` 的插件选择和配置；插件工厂应直接构造具体 Toolset，
不依赖内部 callable toolset 的步骤调度。

## Temporal 与资源所有权

在 Agent 构造时安装动态入口，随后安装 `TemporalDurability`，再由 Worker 的
`AgentPlugin(agent)` 注册。稳定的入口 ID 标识 SDK 的工具发现、校验、调用
Activities；每个插件无需单独包装 `DynamicCapability`。Workflow 中只传本次 deps，
不在 `agent.run` 临时传入 `capabilities=` 或执行型 `toolsets=`。

Temporal 聚合入口可以加载延迟 capability 的 instructions 并启用、调用其工具，但
`load_capability` 返回不包含组内 Toolset instructions：SDK 2.40.0 的 Workflow
工具树不解析动态聚合内部的归属包装，延迟加载器无法收集这部分指令。普通运行保持
完整的 capability 与 Toolset instructions；单个原生 `DynamicCapability` 没有此
缺失。原生 `DynamicCapability` 返回 `CombinedCapability` 时又会在 Activity 访问
live registry 失败；本模块修复该工具归属访问，没有增加另一套延迟指令传输机制。
使用 Temporal 延迟插件时，应将需要加载的规则放在 capability instructions 中。

Workflow 输入保存完整 deps，同一份序列化 deps 供 Activities 使用。普通运行在一个
run 内复用已解析 capability；Temporal 每次工具 Activity 重新解析整个插件集合。
工厂、配置 validators/serializers 及 `for_run` 必须是确定性的纯构造，仅依赖 deps 和
SDK 可序列化的上下文字段，不依赖 Workflow 内的实例、连接或完整消息历史。固定
输入后不能再查询外部配置或环境决定启用集合。

普通 hooks 在 Workflow 侧运行；需要 I/O 时显式调用使用端静态注册的 Activity。
仅给动态构造的方法标注 `@durable_operation` 不会让 SDK 在启动时发现它。本模块
不增加 Activity 注册机制。

| 对象 | 所有者与生命周期 |
| --- | --- |
| Registry、动态入口、Agent | 使用端，通常覆盖进程或 Worker 生命周期 |
| 配置对象、实际 capability | 每次解析创建，不跨 run 共享 |
| Toolset 连接及局部任务 | Toolset 管理；普通运行覆盖运行上下文，Temporal 覆盖单次 Activity |
| 外部业务状态、长期资源引用 | 插件或使用端自己的持久化设施 |

Toolset 在 `__aenter__` 获取资源，在 `__aexit__` 释放；进入失败也应清理已获得的资源，
异常和取消传播。聚合使用 SDK `CombinedToolset`，单次工具 Activity 会进入所有启用
的子 Toolset，不按目标工具延迟进入。跨运行状态由插件自己的存储负责，通过可序列化
业务标识关联资源，处理 Activity 重试和幂等。模块不提供 StateStore、session 清理、
数据库表、迁移或旧插件适配。

## 基础代码与样例

- [新插件基础代码](examples/new_plugin.py)：配置类型和 capability 工厂；使用端以
  `registry.register("my_plugin", config_type=MyConfig, factory=build_my_plugin)` 注册。
- [Echo 插件](examples/echo.py)：配置 `prefix`、`uppercase`，注入 instructions 和
  `example_echo(text: str)` 工具；关闭时两者均不参与运行。
- [普通使用端](examples/in_process.py)：同一 Agent 重配、启用与关闭插件，使用
  `TestModel`，无需外部模型凭据。
- [Temporal 使用端](examples/temporal.py)、[共享 Agent](examples/temporal_agent.py) 和
  [Workflow](examples/temporal_workflow.py)：相同 DTO 和工厂，使用端拥有 Client/Worker。

两个使用端均可通过 `python -m kapy.capability_plugins.examples.<in_process|temporal>`
执行。Temporal 样例需要 `localhost:7233` 的服务。

契约基于项目锁定的 Pydantic AI 2.40.0，参见 SDK 的
[DynamicCapability](https://pydantic.dev/docs/ai/capabilities/custom/#dynamically-building-a-capability)
和 [Temporal Toolset](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/#agent-names-and-toolset-ids)。
