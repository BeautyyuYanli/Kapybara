# Telegram 原生 Rich Markdown

基于 main `39bca06`，仅改 `src/kapy/gateway/telegram.py`、Gateway 测试和模块 README。不改 State、Agent、共享配置、依赖、route 或投影算法，不增加富媒体上传和 stop/cancel。

## 发送与限额

模型正文直接传 `rich_message: {markdown: 原始Markdown}`，私聊及私聊 topic 使用 `sendRichMessageDraft`，所有路由的正式正文使用 `sendRichMessage`。保持稳定非零 draft_id、约 20 秒刷新、重启重发、临时草稿不是正式回执，以及正式 ACK 后推进 offset 的语义。

保留控制回复 `send(chat: int, thread: int, text: str) -> None` 为无 parse_mode 的 sendMessage；增加 `send_rich(chat: int, thread: int, text: str) -> None` 和 `send_rich_draft(chat: int, thread: int, draft_id: int, text: str) -> None`，复用现有节流与 API 调用。命令、配置、创建确认和终态异常提示走 plain；error 产生的整个 pending 使用 plain，保留其已有中间正文而不误解析诊断文字。

官方 [InputRichMessage](https://core.telegram.org/bots/api#inputrichmessage) 要求 markdown/html/blocks 恰好一种；本次只发 markdown，不做 MarkdownV2 转义或完整转换器。[Rich formatting](https://core.telegram.org/bots/api#rich-message-formatting-options) 的上限是 32768 UTF-8 characters（含自定义 emoji 替代文本和公式源）、500 blocks、16 层嵌套、表格 20 列；Rich Markdown 尽可能兼容 GFM，不承诺完全一致。代码采用至多 32768 UTF-8 bytes 的保守原文预算，不将 bytes 宣称为官方字符定义；结构性限制交由 Telegram 校验和受限 fallback，不复制完整解析器。

## 分段与降级

新增内部 `rich_chunk(text: str) -> tuple[str, str]`，返回原文前缀与余文。短正文原样完整发送；长正文只在预算内、普通 fenced code block 外的空行分段，识别反引号/波浪线围栏以免误切代码块。连续非空表格行自然属于同一块，不另写表格解析器。无安全前缀时返回 `("", text)`，调用者将该 pending 未发余文降级 plain，沿现有 4000 UTF-16 units 分段，完整保留源码；不补围栏、复制表头或插入其他 synthetic 字符，不承诺超限块仍以富格式呈现。

草稿直接尝试原始未完成 Markdown，过长预览使用同一安全分段规则；无安全 rich 前缀时使用 plain 草稿预览。明确的富格式拒绝可让本 run 的草稿改用 sendMessageDraft，切换模式时清除发送缓存，避免相同正文跳过 fallback；正式正文仍独立尝试 rich，不因临时未闭合 Markdown 永久关闭最终富格式。plain 草稿 400 保留已有 final-only 降级。

为 `TelegramFailure` 增加默认 false 的 `rich_content_rejected: bool`，只在 rich 方法收到明确 `ok=false`、HTTP/API 400 且描述匹配内容解析或 rich 限额错误的窄分类时设置，不保存或显示原始 description。分类只接受有明确依据的描述，不能仅因包含 rich 就判为格式错误；未识别 400 保留既有失败行为。非内容 400、401/403、429、网络失败、5xx、无有效 API 确认及未知发送结果均不触发换格式重发，继续现有阻塞或重试。明确格式拒绝后，先将 pending 的 plain 模式落盘，再从同一原文 offset 发送；不丢正文，也不重复已确认前缀。

## 持久兼容与覆盖

保留 projection version 1、pending.text 原始正文、pending.next 和 item_offset 的原文 Python 字符偏移。新模型 final pending 增加可选 `format: "rich"`；字段缺失视为旧 plain pending，按原有分段继续，避免重启改变已尝试分段。降级只将 format 改为 plain；offset 永远只计实际确认的原文，不计格式包装。draft 只加本 run 可选 plain 标记；无数据库迁移或第二套 receipt。sendRichMessage 成功但响应或数据库 ACK 丢失仍为 at-least-once，同格式重试未确认分段。

扩展真实 PG 加 mock API 的现有 Gateway 测试：Rich Markdown 原样载荷、短文本、代码/表格边界及超长 Unicode；未完成草稿、稳定 ID/刷新/重启；明确格式拒绝的持久 plain 接续；非格式 400、429、网络未知结果不换格式；raw offset/ACK 丢失重启及已有 version1 plain pending；命令与错误使用 plain。仅在既定非 root Docker、只读挂载和随机 schema/namespace 中验证，不调用真实 Telegram/模型或运行服务。
