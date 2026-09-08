# Telegram thinking 与工具概况

按 `.context/proposals/260908-telegram-thinking-tool-summaries.md` 完成。Pydantic AI 原生 ThinkingPart/ThinkingPartDelta 可直接使用，Runner 只转发其可展示 content，通过已有 notice 和文本分块进入 State；签名、encrypted_content 与 provider_details 不进入预览。Responses/Google 仅在 SDK profile 明确支持时请求原生摘要，不设置思考 effort/budget；Chat 沿用 SDK 已能解析的返回。没有自造供应商流解析器。

Telegram 复用 progress 保存最近 2000 字符的当前 thinking part，增加可选接续标记；正文首个非空 delta 覆盖它，迟到 thinking 不盖住正文。工具、失败、恢复、完整响应结束该预览，旧 v2 投影无需迁移。正文逐 message 正式发送和 pending/ACK 逻辑不变。

工具概况替换了原有 JSON dump：显示名称、主要动作/路径、真实进程状态与退出码、少量文本结果；多行输入/补丁仅显示规模。最多 300 字符、三行并标注截断，未知结构仅显示形状，媒体不展开。running、quiet、timeout、error 和 outcome_unknown 保持实际含义。tool_result 补 name，大包摘要也保留 name；完整 Agent 消息与执行结果不受前端裁剪影响。

修改 Agent models/runner、Telegram、四个测试文件及两份模块文档，无 State DTO/数据库表、provider/session API、工具参数或插件注册变化。实现与方案一致。

流程：一次全上下文 Mei proposal 简化及一次修订；唯一 persistent Elysia 完成实现与补测；独立 Eden 1–2、3–4、5 三组全部 PASS。阶段 4 唯一缺口为长非 BMP thinking 的分块证据，沿用原三协议测试改为 5000 个非 BMP 字符与尾标记，经阶段 3/4 复审通过，未改生产逻辑。

验证：指定非 root Docker Agent/Gateway 233 passed（58.95 秒），含真实 machine 专项；最后长 Unicode 定向 6 passed（2.13 秒）。Ruff/format/diff 通过，Pyrefly 0 errors；保留既有 google-genai DeprecationWarning。实际 SDK mock HTTP 验证三协议 thinking 内容、空/仅签名事件、能力门控；真实 PostgreSQL 验证 thinking 草稿重启和工具概况。无真实模型或 Telegram 测试请求。

限制：只有 SDK 和供应商返回的可展示 thinking 才有内容；不会从加密材料还原或伪造摘要。工具概况是有界展示，完整记录继续由既有历史接口保存。正式发送 ACK 丢失的至少一次边界保持原样。
