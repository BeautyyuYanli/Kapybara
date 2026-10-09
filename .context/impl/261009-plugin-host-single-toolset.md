**PluginHost 单 toolset 简化 · 2026-10-09**

已完成简化，并同步 [proposal](/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/c757dfad-9fae-4e97-abb0-95ce3ee0cc8d/.context/proposals/261008-plugin-host-capability.md) 和 [README](/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/c757dfad-9fae-4e97-abb0-95ce3ee0cc8d/packages/plugin_host/README.md)。

- 插件直接声明 `tools: list[ToolSpec]`，工具名为 `plugin_id__tool_name`。
- 整个 Host 提供一个 `RpcToolset`，每个工具保存自己的客户端、handler、定义和 validator；没有工具时返回 None。
- 删除 `ToolsetSpec`、工具组 ID、包内 `CombinedToolset` 装配及旧分组夹具和校验。
- 内部 hooks 继续使用原生 `CombinedCapability` 排序与调度。

修改集中于声明模型、公开导出、Host 装配、RPC toolset、README 和包内测试。真实 SDK Agent 测试验证多个插件的同名工具分别路由，包含合法双下划线名称与独立 handler；名称冲突仍在加载时拒绝。

**50 项测试通过**；Ruff、格式检查、Pyrefly（0 diagnostics）、`git diff --check` 和 wheel／sdist 构建均通过。cmd-impl 五个审查阶段全部通过。
