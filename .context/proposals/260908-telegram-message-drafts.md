# Telegram 按 message 投递与临时进度草稿

范围为 Gateway Telegram 投影、发送逻辑、相应测试和模块文档。复用 State 现有 records 和 Bot API 的 Rich Markdown，不改 Agent、模型供应商、session 输出协议或数据库表。

私聊及私聊 topic 的 draft 对应当前正在形成的一条 message。工具调用、工具返回、失败重试和中断恢复作为临时进度显示；用统一的文字摘要显示工具名、参数或结果，最多 2000 字符，截断时明确标记，不带 session/attempt/call ID，不直接展示原始异常。只保留最新一条进度，不另存事件列表或按工具定制格式器。进度以 plain draft 发送，模型正文继续使用原生 Rich Markdown。多个中间事件更新同一草稿；下一条 message 的首个非空 text_delta 清除进度，草稿中只保留该 message 的正文，后续 delta 按 part_index 累加。未输出正文的空 delta 不抢先抹掉进度。

`model_response` 是完整 message 的持久边界。遇到有正文的完整响应，立即冻结该 message 的正式 pending，并停止处理本页剩余 records；无需等到整个 run 的 final/waiting。正式消息确认后释放当前 draft，之后的工具状态或下一 message 使用新 draft ID。群组没有 draft，仍在每条 message 完成时发送正文。空 model_response 不发送空消息；不完整正文在失败或中断时撤回，仅显示自然的恢复状态。已正式发送的 message 不因后续尝试失败而撤回。

沿用唯一 pending 的 text/cursor/next 和 item_offset：先持久化再发送，逐段成功才推进 offset，pending 清空前不读取后续正文。长 message 沿用 rich/plain 分段及格式错误降级。ACK 丢失可能重发未确认分段，不能声称 exactly-once。每次正式发送结束后丢弃该 message 的正文缓冲，仅保留当前 run 最后一条已确认响应的正文指纹，避免 final 再发同一答案；内容不同的 final 文本单独发送，没有 model_response 的 final 文本也正常发送。final/waiting 本身不渲染日志，结构化 session output 只用于判断结束，完整 DTO 在 State 中保持原样。终态失败单独发送现有安全错误说明，不拼接已发正文。

投影改为 version 2，复用现有 JSONB；保存当前 message 的 ID/parts、最新进度、draft、当前 run 与最后已发响应指纹，不保留整轮 message 集合或另一套待发队列。空初始投影可以建立 v2；非空旧版投影明确拒绝自动转换，不能清空未确认 pending 或倒退 cursor 重播旧消息。每页在实际处理位置持久推进，重启恢复同一 pending/offset 或同一 draft。发送格式由当前展示内容决定，rich 拒绝后的降级仅限当前正文草稿；缓存同时比较正文与实际发送格式，保证进度 plain → 正文 rich 时立即更新。正式确认后生成新的草稿，重试与刷新仍沿用已有节流和约 20 秒刷新。

参考 Telegram 官方 [Streaming Replies](https://core.telegram.org/bots/features#streaming-replies) 与 [sendRichMessageDraft](https://core.telegram.org/bots/api#sendrichmessagedraft)：draft 是临时展示，正文使用正式发送接口持久化。
