已完成 PluginHost 的运行时插件选择支持，并更新 [proposal](/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/c757dfad-9fae-4e97-abb0-95ce3ee0cc8d/.context/proposals/261008-plugin-host-capability.md) 和 [用法示例](/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/c757dfad-9fae-4e97-abb0-95ce3ee0cc8d/packages/plugin_host/README.md:31)。

- `describe_plugins()`：通过 RPC 获取可序列化的 manifest 快照。
- `build_plugin_host()`：同步校验、复制快照并绑定客户端，不执行 RPC。
- `load_plugin_host()`：复用上述两步，保留便捷加载入口和原始客户端对应关系。

Agent 定义时注册固定 ID 的原生 `DynamicCapability`，每次 `.run()` 根据 deps 快照生成 Host，instructions、hooks 和工具一起切换。快照支持 JSON 往返和重复构建，后续修改声明不影响已有 Host；单 toolset 和内部原生排序保持。

独立包提供这层构建接口；Temporal 的声明发现 Activity、instruction/hooks RPC 代理和 Worker 注册由调用方提供，README 已说明边界与用法。

**53 项测试通过**；Ruff、格式检查、Pyrefly、差异检查和 wheel/sdist 构建通过。`cmd-impl` 五个审查阶段全部通过；审查中删除了重复测试断言，并补齐直接构建校验及发现 RPC 的错误、取消传播验证。
