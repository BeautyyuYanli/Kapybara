# Telegram Rich Markdown 实现报告

分支 `feat/kapy-telegram-rich-markdown`，代码验证提交 `a81f9d01876b482f6e888534bceed72d791bcdab`，已合入 main `ec34cc0` 的真实内容拒绝证据。

模型原始 Markdown 直接发送至 sendRichMessageDraft/sendRichMessage，由 Telegram 渲染；命令与错误保持 plain。复用既有投影、route、节流、草稿 ID/刷新和 ACK 逻辑。只修改 owned Gateway 源码、测试和模块 README，没有 State/Agent、共享配置、依赖或运行服务变更。

新增保守 32768 UTF-8 bytes 原文预算及普通 fence 外空行分段，保留 CRLF 和 Unicode 原始字符偏移。无安全 rich 块时，未发余文完整转 plain，不插入围栏、表头或其他 synthetic 字符，不实现完整 Markdown parser。version 1 pending 缺 format 时继续旧 plain；新 final 独立选择 rich，草稿降级清缓存并支持重启。

相对最初实现，删除全部猜测英文解析/限额错误模式，仅接受 architect 在 ec34cc0 记录的四个实测精确码：RICH_MESSAGE_TEXT_TOO_LONG、RICH_MESSAGE_BLOCKS_TOO_MANY、RICH_MESSAGE_TABLE_COLS_TOO_MANY、RICH_MESSAGE_DEPTH_INVALID。必须同时满足 rich 方法、HTTP 400、API error_code 400 和 ok=false。明确拒绝后先持久化 plain 模式再投递；未知 400 保留 pending，429、网络错误及未知发送结果不切格式。原始 description、URL 和凭据不持久化或记录。

同一 persistent Elysia 完成实现及返工，新任务 Eden 1–2、3–4、5 全部通过。审查关闭了猜测错误文案问题，补强同实例同正文 rich→plain 草稿缓存切换，以及 HTTP/API 状态独立判定测试。

最终 Docker Gateway 回归 81 passed、零 skipped，38.90 秒，启用 KAPY_DOCKER_TEST=1；ruff check/format 通过，pyrefly 0 errors。全部检查在 kapy-v2-machine:dev 内以 UID/GID 10001、cap-drop ALL、no-new-privileges、init、kapy-v2_default 网络、2 GiB/256 PID 和只读 src/tests/pyproject 挂载执行，使用随机 PostgreSQL schema/Valkey namespace、mock Telegram/模型。未读取主 .env 或调用真实 Telegram/模型。

超限不可分块呈现为完整 plain 源码；未验证的其他格式错误保持失败，不猜测降级。正式发送成功但响应或数据库 ACK 丢失仍为 at-least-once，可能重复未确认分段。最终集成、真实 Bot 验证与上线由 architect 执行。
