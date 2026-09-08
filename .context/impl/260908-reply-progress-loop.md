已完成连续回复模型，并通过 cmd-impl 五阶段审查。

`reply_to(ids)` 现在即时提交所选回复，返回完整 `ReplyResult` 和剩余待回复地址；仍有待回复输入时继续同一个 Pydantic AI run。全部已读输入完成、整批工具及 steer 边界处理妥当后，返回正常 `AgentRunResult.output` 结束。text 模式最终文本自动回复全部已读未回复输入，`wait_for` 保持暂停语义。

新增 `RunContext.reply`，复用现有 PostgreSQL 事务和 records 实现原子结算、幂等重放与即时交接。已回复结果不会被后续失败覆盖；接入新输入会清除旧批次的结束候选。模型仍只传 ids，运行时填充 payload，历史与压缩保留每次完整 output。

删除旧的终结型 reply_to 路径，cycle 的单值 output 改为 outputs，快照升级为 `kapy.agent.v3`，不兼容旧快照。产品说明、接口和运行文档已同步。

相对提案唯一接口扩展是可选的同步 `validate_receipt` 回调：State 在提交及幂等重放时，调用 Runner 校验真实完整回执消息，随后复用已校验消息落盘，避免回复已提交但回执超限。未新增服务、表、公开 RPC 或依赖。

验证：非 root Docker 中全部非 live 测试 **402 passed**；最后仅整理工具结果分支后，全部 Agent 测试 **102 passed**。覆盖回复交接、活动等待、批次尾随工具、原子拒绝、恢复与完整输出压缩；模型和 Telegram 使用 mocks。临时测试资源已清理。

Ruff、格式与 diff 检查通过。Docker 生产源码及 `uv run --locked pyrefly check` 均为 **0 errors**；后者提示忽略 tests。Docker 全项目检查仍有 **43 个测试类型错误**，同环境基线为 74，新增回复测试无错误；未修改配置掩盖这些诊断。
