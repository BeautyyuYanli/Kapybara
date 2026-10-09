已完成独立包 [PluginHostCapability](/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/c757dfad-9fae-4e97-abb0-95ce3ee0cc8d/packages/plugin_host/README.md)：插件收缩为 hooks 与静态/动态 instructions，保留原生 ordering；删除 ToolSpec、插件工具和 toolset。

Agent 定义时注册固定 Host，`for_run(ctx)` 从 deps 中的 manifest 快照创建隔离运行实例。固定 SDK 绑定随实例复制，插件选择、导出表和回调重新组装。动态 instructions 每次模型请求求值。

hooks 和动态 instructions 的 RPC 共用唯一 `invoke_plugin` durable operation，客户端只在方法体中获取。普通 Agent 直接调用；Temporal Workflow 通过 SDK Activity 执行。远端不接收 ctx。发现插件、Worker 和 RPC 客户端生命周期由调用方管理。

验证：46 项测试通过，包含真实 Temporal 并发执行、12 次 Activity RPC 和两份历史重放，重放未增加 RPC 或客户端查找次数。Ruff、Pyrefly、wheel/sdist 构建及五阶段评审全部通过。评审中修复了合法根 `$ref` schema 被误拒的问题，补齐引用校验回归并删除冗余测试；无方案偏离。
