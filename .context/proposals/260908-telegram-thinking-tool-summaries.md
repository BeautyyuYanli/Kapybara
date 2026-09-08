# Telegram thinking 与工具概况

在当前逐 message 草稿流程上做增量修改。工具调用和返回显示简短、有用的概况；Pydantic AI 已支持的可展示 thinking 文本进入临时 draft。保持正文完成即正式发送、首个非空正文 delta 覆盖中间状态、Rich Markdown、pending/ACK 和恢复语义。

Runner 复用 Pydantic AI 的 ThinkingPart.content 与 ThinkingPartDelta.content_delta，通过已有 notice 输出 `{kind:"thinking_delta",part_index,text}`，沿用 message_id、attempt_id；参数化现有文本分块循环，不增第二套分发器。忽略空文本、signature、encrypted_content 和 provider_details，不解析供应商原始流或解密内容。Responses adapter 在 SDK profile 明确支持 reasoning 的模型上设置原生 openai_reasoning_summary="auto"；Google adapter 在 SDK profile 支持 thinking 时使用原生 include_thoughts=true，仅请求展示内容、不改 effort/budget；Chat 接收 SDK 已能解析的 thinking，不新增请求字段。未知能力或供应商不返回内容时保持原有工具状态，不补写假思考、不增加自定义适配器。模型协议与能力判断仍属于 backend，Telegram 不识别供应商。

Telegram 复用 v2 projection.progress 保存 thinking 展示文字，仅增加当前 message_id、part_index 标记供接续；只保留最近 2000 字符的窗口，不复制正文或保存整组推理列表。新的 part 更新当前预览。它以 plain draft 展示，正文首个非空 delta 到来后清除；同一 message 已开始正文时迟到的 thinking 不盖住正文。工具状态、失败、恢复及完整响应也结束该临时预览；空或仅签名事件不会擦掉现有内容。现有 v2 投影无需重置或迁移，完整消息的模型恢复材料仍由原 codec 保存。

工具调用概况包含工具名和少量主要参数，例如 `process_start · git status`、`read_media · ./plot.png`。使用有限的通用字段选择与文字裁剪，命令/路径保留一小段，多行输入或补丁只显示行数/字符数；不展示调用 ID、session token、完整 JSON 或大段代码。返回概况优先展示实际结构中的进程状态、退出码及一两行已解码输出，例如 `process_start · exited · exit 0 · working tree clean`。running/quiet/timeout 不写成成功结束；错误和 outcome_unknown 明确保留相应状态。普通文字结果取短片段，列表显示条目数，未知结构只显示简短形状；媒体及 base64 不展开。不使用模型生成摘要，不按插件名维护独立格式器。

现有 tool_result 事件数据补上 Runner 已知的 name，Telegram 可独立处理 return，不另建 call ID→工具的持久映射。概况只读取直接字段及已知 process/output 形状，不递归遍历任意 JSON。超出既有 delta 上限的结果继续采用已有摘要，保留可用工具名，不回读完整 payload。工具概况合计至多 300 字符，最多三行，截断明确标记；完整参数/结果仍留在 State 原始记录与 Agent 上下文，前端裁剪不影响执行或恢复。旧记录缺 name 时显示通用工具返回提示。

范围为 Agent 的 SDK 流事件/backend 设置和工具结果 envelope、Telegram 投影与概况函数、对应测试及模块文档。不改 State DTO/表、provider/session 配置 API、插件机制或公开工具参数。

依据：已安装 Pydantic AI 提供上述类型及 settings；OpenAI 官方 [Reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning#reasoning-summaries) 要求显式请求 summary，返回可展示摘要，模型支持范围不同。
