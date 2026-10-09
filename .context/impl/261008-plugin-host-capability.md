**PluginHostCapability 实现记录 · 2026-10-08**

已更新 [proposal](/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/c757dfad-9fae-4e97-abb0-95ce3ee0cc8d/.context/proposals/261008-plugin-host-capability.md)，完成独立包 `kapy-plugin-host`，可通过 `import plugin_host` 使用。cmd-impl 的五个审查阶段全部通过。

本次新增与修改：

- 新增 [独立包及 README](/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/c757dfad-9fae-4e97-abb0-95ce3ee0cc8d/packages/plugin_host/README.md)，公开 `PluginHostCapability`、`load_plugin_host`、`RpcClient`、声明/响应模型和 `PluginHostError`。
- 实现静态与动态 instruction、JSON Schema toolsets，以及 14 个普通 hooks。动态逻辑统一调用 RPC，插件接收显式 JSON 数据；本地 ctx 留在 SDK 适配层。
- [Host](/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/c757dfad-9fae-4e97-abb0-95ce3ee0cc8d/packages/plugin_host/src/plugin_host/capability.py:60) 作为一个 `AbstractCapability` 参与外部排序，内部 `CombinedCapability` 使用原生规则排序和调度 hooks。默认 visitor 保持 Host 不透明，内外 callbacks 不交错。
- 根 `pyproject.toml` 登记 workspace 成员，`uv.lock` 由 uv 更新，SDK 保持 2.40.0。既有业务代码无删除，本次交付范围为独立包。

相对提案的必要实现调整：工具集合由 Host 直接返回原生 `CombinedToolset`，使 SDK 将工具所有权绑定到外部可见的 Host；instruction 和 hooks 仍委托内部组合。该分工解决 SDK 2.40.0 内部工具 owner 必须出现在外部 capability registry 的约束。另转发公开的 `has_on_event`／`listens_to`，保持 SDK 的事件监听与 streaming 判断。

`jsonschema` 下限收紧为 4.18，以使用原生 schema resource walker；包内增加独立测试、类型检查依赖。引用检查包含实际引用目标、合法循环和空字符串同文档引用。参数替换与恢复再次验证 schema；provider 扩展设置、运行时注册事件、严格有限 JSON、取消传播与错误上下文均有回归覆盖。

验证结果：

- [包内测试](/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/c757dfad-9fae-4e97-abb0-95ce3ee0cc8d/packages/plugin_host/tests/test_plugin_host.py)：**48 passed**。测试通过真实 SDK Agent 验证两层排序、工具执行与恢复、动态 instruction 和结构化输出；使用确定性模型。
- Ruff 检查与格式检查通过；Pyrefly **0 diagnostics**；`git diff --check` 与 `uv lock --check` 通过。
- sdist／wheel 构建通过；全新 Python 3.14 环境中的独立 wheel 安装与 Agent 执行验证通过。

包内测试命令：

```sh
uv run --frozen --no-sync pytest -q --tb=short -c packages/plugin_host/pyproject.toml packages/plugin_host/tests
```

审查补齐了 schema 引用目标检查、hook 数据适配和错误身份边界，并增加三条真实 Agent 用例，覆盖原始参数修复、工具错误恢复和结构化输出修复。最终代码、测试与文档均通过复审。

调用方提供并管理 RPC 客户端、传输和插件进程；Host 借用客户端。Kapy 业务运行链路接入留待后续任务。
