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

7. Kapy 每次 Agent run 显式传入 `conversation_id=str(session_id)`；工具和 SDK hooks
   从 `ctx.conversation_id` 读取身份，不从 State 或 Kapy deps 身份字段推断。

8. Config 仅保存固定配置及外部借用资源的定位信息。State 仅保存业务 JSON 进度。
   配置验证、从 `{}` 初始化默认 State 和序列化均在本地完成。

9. `enter_conversation(conversation_id, config) -> R` 仅从 Config 水合本地句柄，
   `exit_conversation(resource) -> None` 仅关闭该句柄。两者在数据库事务外执行。
   State 由业务工具和 hooks 维护，长生命周期外部资源由外部系统或其编排方管理。
