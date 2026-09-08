# Telegram 按 message 投递与临时进度草稿

已按 `.context/proposals/260908-telegram-message-drafts.md` 完成实现。工具调用、返回和恢复状态只更新最新 plain draft；下一条 message 的首个非空 delta 将它替换为 Rich Markdown 正文。每条完整 model_response 立即进入正式发送，确认后结束当前 draft，后续输出使用新 draft。

删除原有整轮 message 集合与终态拼接。投影 version 2 只保留当前 message、最新进度、draft 和最后已确认正文指纹，继续使用唯一 pending 与原文字符 offset。final 不重复已确认正文；不同尾部结果正常发送。失败只替换未完成草稿，已发 message 保留，终态错误单独说明。Rich 分段、精确内容拒绝分类、限流与 ACK 重试复用现有实现。

范围为 Telegram 模块、Gateway 测试及模块 README，未改变 State、Agent、provider、公共接口或数据库表。与方案一致；审查额外删除最后响应指纹旁无读者的 ID 字段，并修正文档中旧的 run 范围降级描述。

cmd-proposal 完成恰好一次全上下文 Mei 简化审查与一次简化。cmd-impl 复用唯一 persistent Elysia，Eden 按 1–2、3–4、5 三组独立上下文执行；全部五阶段通过。阶段 1 删除无用 ID 后复审通过，阶段 5 修正两句文档后复审通过，其他阶段无需返工。

验证：非 root Docker Gateway 114 passed（50.70 秒），启用真实 machine 专项；最后补强的失败恢复定向检查 1 passed。全仓 Ruff、Pyrefly、格式和 diff 检查通过。覆盖逐条发送、首 delta 替换、不同 draft ID、多 part、跨页、PostgreSQL JSONB 重启、长 pending、第二条消息 ACK 丢失、重复正文跨 message/run、进度上限和失败后保留已发正文。没有发送真实测试 Telegram 消息或调用模型。

非空旧投影保持原样，须确认旧版投递已排空后保留 cursor 转换；不会自动重播或丢弃未确认内容。Telegram 与数据库无共同事务，正式发送成功但 ACK 丢失时仍可能重复未确认分段。
