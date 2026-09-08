已实现新的 session 交互模型，完成 cmd-impl 的五个审查阶段。

- 创建时固定 `output_mode=text|reply_to`。普通模式使用正常文本或非空 `wait_for`，不向模型注入 being_waited_id，也不暴露 reply_to 工具或提示词。
- 显式模式只通过 `wait_for` 或 `reply_to(ids)` 结束。输出函数用最近的模型正文补齐 `ReplyTo.payload`，完整 DTO 成为 Pydantic AI output，并统一进入恢复状态、历史压缩和结果交接。
- 每条直接输入自动获得独立回复地址，steer 与 queue 都适用。等待结果不会产生新的回复义务；输入消费与回复完成分开记录，等待期间保留未回复输入。
- 通道改为一次性、一对一。文本结束结算已消费未回复输入，reply_to 原子结算指定输入；删除默认通道、自订阅、广播及重复投递路径。
- Gateway、CLI、Telegram、接口文档、产品说明和递归示例已同步。移除 create/input 自选 waiting_id，增加创建参数 `--output-mode`。

相对提案，按用户后续要求直接删除旧模式兼容层：不转换旧提示词、快照、输出、通道或授权记录。Runner 只接受 `kapy.agent.v2` / version 2；数据库与 Gateway 遇到旧业务状态会明确拒绝并保留数据。没有自动清空数据的路径。本次继续使用现有 State、PostgreSQL、Valkey 和进程结构，未新增服务或依赖。

恢复时复用 Pydantic AI 的完整输出参数校验，保持正常运行与中断恢复的出口选择一致。回复提交前验证完整 waiting 输入的编码大小，避免生产方完成后接收方无法交接；完整 output 不截断、不拆成单独正文。

最终 Docker 联合回归为 **265 passed，131.47 秒**，覆盖 Agent、State、Gateway、CLI，包含并发、批量删除、真实机器 CLI、一次性交接、非法回复目标的事务拒绝、跨轮失败以及重启压缩后的回复。

```sh
docker compose -p kapy-session-output-impl --profile dev run --rm --no-deps machine \
  python -m pytest tests/agent tests/state tests/gateway tests/cli -q -p no:cacheprovider
```

Ruff、格式和 `git diff --check` 通过。Pyrefly 为 0 errors；项目配置跳过 tests，另有 2 个 suppressed、12 个未展开 warnings。测试工作负载在 UID/GID 10001、移除 capabilities、禁止提权的 Docker 容器中运行；供应商推理与 Telegram 使用 mocks，没有真实调用或发送。
