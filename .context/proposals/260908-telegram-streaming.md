# Telegram 自然聊天与原生草稿

基于 main `35016ec`，仅修改 `src/kapy/gateway/telegram.py`、相关 Gateway 测试及模块 README；复用现有 delivery JSONB，不新增表、依赖或公共业务接口，不改 State、Agent、共享配置。

## 呈现与接口

当前 `project()` 将 delta、tool、waiting 和 attempt 日志累加，`deliver_once()` 每页逐块 `sendMessage`，导致一句 Hello 变成多条带 session 前缀的消息。改为一个 run 内按 message 顺序形成一条逻辑回复：生成时私聊持续更新同一草稿，终态持久发送完整正文；长正文按段落优先、最多 4000 UTF-16 units 分段。群组及其 topic 只发送终态正文，避免碎片。私聊 topic 仍用原生 draft；从已持久 inbox 的 `chat.type` 保存路由类型，旧路由无类型时用 `getChat` 补齐，不以 thread 是否为零判断私聊。

保留 `send(chat: int, thread: int, text: str) -> None`；新增 `send_draft(chat: int, thread: int, draft_id: int, text: str) -> None`，共用 chat 锁、节流与 retry_after。Gateway 内部 `project(records: list[dict[str, Any]], previous: dict[str, Any]) -> tuple[str, dict[str, Any]]` 保留签名，首项改为当前预览快照，正式待发内容从版本化 projection 读取。

普通正文去掉 session 前缀、waiting/tool/attempt 日志；首次普通输入创建 session 不另发通知，显式 `/new` 简短确认。帮助、状态命令可展示诊断 ID。真实终态 error 必须自然说明本次未完成，保留 State 提供的安全错误类别，不暴露原始异常；已完成的中间正文保留，失败草稿不冒充结果。

## 投影与持久化

复用 `gateway_telegram_delivery` 的 cursor、projection、item_offset、重试字段，以及唯一的 `projection.pending = {text, cursor, next}` 待发结构。projection 增加 version、当前 run 的有序 message 正文/分 part 的临时 delta、active draft（非零 ID、最近内容/刷新时间、本 run 不可用标记）；完成后释放本 run 累积数据。cursor 表示已持久投影的读取位置，item_offset 只表示正式 pending 已确认发送的字符位置，不新增待发队列或分段回执集合。

按 run_id、message_id 归属正文，按 part_index 拼接 delta；`model_response.text` 覆盖该 message 的临时文本。多个模型响应、工具前后的自然文本按原顺序保留；忽略 tool 数据和诊断 notice。attempt_failed 仅撤销对应失败 message 的临时正文，后续修正替换草稿；State 的 interrupted 是同 run 恢复标记，只撤销未完成 attempt，不当作终态失败。`final.output` 与末条已完成正文相同则只保留一次，不用全局文本去重吞掉下一 run 的相同答案；不同则保留其他中间正文并采用 final 结果。error 形成自然失败结尾，随后的 waiting 不重复发消息；waiting 本身不产生正文。

逐页持久化投影，遇终态即冻结该 run 的 final pending，先投完再继续后续 run；不把分页边界当消息边界，也不再以“只留 8 条 message”丢弃尚未完成的正文。只用 durable State 正文修正 delta；不再对最终正文静默截断到 256 KiB。超长预览仅展示可容纳的一段，完整内容等终态分段发送，不为了续流提前发送不可撤回的正文。

草稿发送前保存 ID 与内容；沿用现有 delivery 循环，内容变化时合并更新，工具等待时约 20 秒刷新已有草稿，不另开刷新任务。发送草稿成功绝不是 final receipt，失败或过期也不能推进正式 offset。终态先落盘 pending，再逐段 sendMessage，成功后落盘 offset；重启从未确认分段继续。私聊 draft 的 400 不阻塞最终正文，记录本次 draft 不可用并降级终态发送；429/临时故障沿现有退避重试，403/401 保留现有禁止投递/禁用语义。正式发送失败保留 pending 和错误状态，不能静默清掉。

同 route 的多个 session delivery 按创建对应 inbox update_id 排序，较早 session 尚有运行中的 run 或未投递正文时后续回复不能抢先；run 终态且 pending 清空即释放 route，waiting session 不永久阻塞新 session。不同 route 可继续。读取 inbox 的持久 offset、已解析 action 的幂等键、allowed chat 限制保持原语义，不新增调度框架。

## 旧投影接续与边界

无版本 projection 由一次性转换函数处理并原子保存新版本。已有 cursor 之前仅恢复当前未完成 run 必需的正文上下文，不重建或补发历史 completed run。旧 pending 按原有渲染规则从 cursor 到 pending.cursor 重建带来源的文本片段，忠实包含旧 256 KiB 限制、纠正文案及八条 message 裁剪，再以原 item_offset 切分，剔除 Gateway 生成的日志，只接续尚未确认的正文；不能用正则删除可能属于用户正文的方括号。仅为当前 legacy run 保存已交付正文前缀：final 可延续则补尾部，否则发送一次自然说明的完整修正版；该 run 投完即删除兼容字段。旧 pending.next 不冒充发送回执，正常投递不保留两套渲染路径。

Telegram 与 PostgreSQL 无共同事务：sendMessage 已成功、响应或本地 ack 丢失时，重试可能重复该分段，保证仍为 at-least-once；草稿 ID 不能提供正式消息幂等。已被旧版截断且记作完成的历史不主动重播修补。

官方核对（2026-09-08）：[sendMessageDraft](https://core.telegram.org/bots/api#sendmessagedraft) 仅面向私聊，支持 message_thread_id、非零 draft_id，同 ID 动画更新；0–4096 字符，空文本显示 Thinking；草稿约 30 秒，须 sendMessage 持久化。[Streaming Replies](https://core.telegram.org/bots/features#streaming-replies) 明确上述生命周期。本次不启用可选 can_stop/keep_on_stop，不引入取消接口或 rich message 系统。
