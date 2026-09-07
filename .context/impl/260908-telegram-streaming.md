# Telegram 自然流式实现报告

实现完成于 `feat/kapy-telegram-streaming`，代码提交 `21e01bca132b2ee0953899c87de0d6ca2d508730`。修改仅涉及 Telegram Gateway、相关测试与模块文档，未改 State、Agent、公共业务 API 或共享配置。

私聊及私聊 topic 使用稳定非零 draft ID，内容变化与约 20 秒刷新均复用现有投递循环，重启后重新发送草稿；群组及群 topic 仅发终态。正式正文按 4000 UTF-16 units 分段，不截断长正文；去掉运行日志、普通 session ID 和隐式创建通知，保留自然失败提示。

复用 delivery JSONB 的版本化投影、pending 和确认 offset。显式 message_order 保证 PostgreSQL JSONB 往返后的正文顺序；part 按数值排序，失败临时正文撤销，完整中间响应保留。final 合并按末条完整响应归属处理，空响应不会吞掉更早正文。终态 cursor 只覆盖实际处理记录；分页、同页后续 run、相同答案及同 route 会话顺序保持正确。

相对最初 proposal，按 architect 裁决删除全部旧 renderer 复刻、历史重建与已交付前缀接续设计。`empty_projection()` 精确返回 `{"version": 1, "messages": {}}`；非空无版本旧投影明确要求离线排空迁移，不自动重置。architect 停止旧 control 并确认无 active run、delivery 已到输出末尾、无 pending/offset 后，保留 cursor 转换；竞争输入或待发存在时恢复旧版排空。

cmd-impl 使用同一 persistent Elysia，Eden 1–2、3–4、5 三组全部通过。审查修复了 JSONB 消息顺序及末条空响应两项实现缺陷，删除误导性长循环测试，并补齐终态前失败草稿修正、多 part 持久化及超过八条消息重启覆盖。

最终 Docker Gateway 回归：44 passed、1 skipped；ruff check/format 通过，pyrefly 0 errors。跳过项为未启用 `KAPY_DOCKER_TEST=1` 的真实 machine 专项。检查在 `kapy-v2-machine:dev` 中以 UID/GID 10001、cap-drop ALL、no-new-privileges、init、bridge `kapy-v2_default`、2 GiB/256 PID、只读源码挂载执行，使用真实 PostgreSQL/Valkey 的隔离 schema/namespace 和 mock Bot API。未读取主 .env，未调用真实 Telegram/模型，未操作运行服务。

正式 sendMessage 成功但响应或本地确认丢失仍可能重复未确认分段，边界为 at-least-once；临时 draft 不是正式回执。真实集成验证、受控投影转换和上线由 architect 完成。
