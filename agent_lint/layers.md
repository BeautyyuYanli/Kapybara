---
globs:
  - "src/kapy/layers/**"
---

# 业务 layer 规则

业务 layers 在 [layer registry](../src/kapy/layers/registry.py) 中统一组装，必须遵守以下约束：

1. 修改运行中的 history 时，只能修改已经带有 `metadata.seq` 的部分；其后尚未分配
   `seq` 的消息必须保持原样。尚无已分配 `seq` 时，不得修改 history。

2. 最后一个已分配的 `seq` 数值在修改前后必须相同（对应消息内容可以变化）。
   保留的 `seq` 必须合法且严格递增，不得擅自为未编号消息分配 `seq`。

3. 修改 history 后，必须保持消息结构及工具调用/回执配对有效，尤其不能破坏跨越
   已编号/未编号边界的配对。

4. 所有业务 capability 定义的方法都可能被重试。hooks 不要求严格幂等，可酌情允许
   非 critical 的重复副作用，但重试不得造成 critical 的重复业务效果。

5. 工具只能响应模型的显式调用，并且执行前，该调用必须已记录在 model response
   末尾保存的 snapshot 中；不得从 hooks 或其他路径隐式调用工具。

6. 工具实际执行必须与快照中的工具身份、调用标识和业务参数语义一致，不得静默
   改变已记录调用的语义。

7. 业务 layer 的 tool 或 hook 创建上游资源时，上游支持则应为资源或其 scope
   设置会话归属标识。标识根据当前会话的 `conversation_id`、`layer_id` 和固定
   Config 按需确定或派生，只使用定位本 conversation 资源所必需的信息，
   不得由模型指定或覆盖。

8. `exit_conversation` 应能重建归属标识，找全并回收所属资源，包括未写入 State
   的资源。

9. 上游不支持按归属查询或清理时，可依赖 State 尽力回收，但须在具体实现
   中说明可能遗漏资源并留下孤儿资源的局限。

10. Kapy 每次 Agent run 显式传入 `conversation_id=str(session_id)`；工具和 SDK hooks
    从 `ctx.conversation_id` 读取身份，不从 State 或 Kapy deps 身份字段推断。

11. `enter_conversation(conversation_id, config)` 只在创建时初始化 State。

12. `exit_conversation(conversation_id, config, state)` 用于显式关闭和创建失败补偿。

13. `enter_conversation` 和 `exit_conversation` 是独立回调，没有 RunContext，
    由宿主传入同一个字符串身份。

14. State 只保存业务进度及必要的回收记录。

15. 配置校验、默认 State 初始化与序列化无外部 I/O；enter 可执行业务 I/O。
    无 enter 时 State 必须能从 `{}` 初始化。
