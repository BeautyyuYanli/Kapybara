# Kapy v2 对齐审查

基线 `ca22ab7`。本轮直接对照 `kapy_v2.md`、当前源码和既有验收记录；以用户最新的 Docker、非 root、best-effort 清理、API usage 及 OpenAI-compatible 澄清为准。历史 senior 消息不作为新需求。

| 需求 | 实现证据 | 判断 |
| --- | --- | --- |
| 控制面与执行面分离，执行机主动双向 WS、本地 CLI 代理 | `gateway/app.py`、`execution/daemon.py`、`rpc/peer.py`、`execution/client.py` | 主架构吻合；没有将机器执行搬进控制面 |
| session 串行、跨 session 并行，steer/queue、持久历史与恢复 | `state/service.py` 的 coordinator、prepare、poll、checkpoint、finish；`agent/runner.py` 的持久工具边界 | 主链路吻合；PG 权威状态、Valkey hints 分工合理 |
| MPMC、早到事件保留、消费后靠输入持久化、自排除 | `state/service.py:_deliver/_event/_finish/_completion` | 在同一短写事务内持久投递；own input 永久订阅，其他订阅持续到下次替换，符合澄清 |
| 递归 CLI 与非消费 completion 观察 | CLI session create/input/wait、Gateway completion_channel、State wait_submission | 功能存在；既有真实递归验收依赖任务中逐条给出 CLI 和 wait 操作，基础提示缺少这部分使用说明 |
| stdio 完整保存、PTY 尾窗、非致命超时、分块文件 | `execution/processes.py`、`files.py`；已有真实 Docker hash/PTY/重连验收 | 机制吻合；逃逸后代不保证清尽属于用户接受的边界 |
| 历史受限 SQL 与多语言关键词检索 | `state/history.py` 的 AST 白名单、session history 子关系、Unicode/CJK tokens | 无明显越界；不是任意 PostgreSQL 和语义搜索，属于已批准范围 |
| skill 快照与 CRUD、脚本插件、apply_patch 生成资源 | `skills/`、`gateway/skills.py`、`agent/machine.py`、`generate_apply_patch.py` | Skills 与生成资源机制吻合；但 Runner 自动注册 apply_patch、MachineTools 按名字特判安装，尚未真正满足统一插件注入，必须修正 |
| 媒体拒绝恢复、API usage 压缩 | `agent/codec.py`、`payloads.py`、`runner.py`、`compression.py` | 不重读变化媒体路径、不靠本地 token 估算；层级投影与原始历史分开，符合澄清 |
| 模型和服务商可替换 | Runner 直接 import/构造 OpenAIChatModel；Settings.require_control 强制 openai_api_key | 实质耦合。按最新要求先抽离 OpenAI-compatible 适配器和中性配置，不增加其他原生协议 |
| 前端接口化、Telegram 是首个插件 | 已有 Frontend.run，但 Principal/recover/Metadata migration/delete 含 Telegram 分支；Telegram 直接访问 control.sessions | 实质耦合。新前端不能仅实现接口，需修改核心身份、恢复与清理 |
| agent 视角的 prompt/tools | BASE_INSTRUCTIONS、BUILTINS、Pydantic 参数 schema | 说明不足：递归回执、当前机器、PTY/stdout cursor、Ctrl-C 与输出展示截断未完整说明；kill 文案误称 tracked descendants |
| 模型工具范围 | `agent/machine.py:file_read/file_write` | 额外文本工具不在原始设计中，用户明确要求删除。它们应回归进程命令与 apply_patch，底层字节传输保留 |

UTF-8 复现在非 root、无网络 Docker 中调用真实 MachineTools，文件内容 `甲乙丙`，limit=4：第一页 offset 0→4、第二页 4→8，两页 text 均为 `[This range is binary or splits a UTF-8 character; choose another range.]`。这是本轮新复现，不把既有测试总数当作本轮验证。

上述复现暴露了额外工具的缺陷，但修补它会延续工具范围偏离。按用户明确裁决，移除 file_read/file_write，不实施 UTF-8 分页修复。

结论：没有发现需要推翻 State/Execution/Skills 分层的明显偏轨。首批修正聚焦模型适配、前端边界、agent 使用契约及删除多余文本工具。单控制进程、逻辑 session 隔离、限制型 SQL、stdio 展示有界但底层完整、API usage 未知时不估算、Telegram ACK 丢失可能重复，均为已明确的产品边界，不另建框架替换。

本审查是源码与代表性故障路径检查，不声称穷尽所有故障。State 失去租约后停止接单属于 fencing；当前 TCP 容器健康检查不能代表完整业务健康，应在运行状态可观测性后续工作中单独处理，不能因此宣称具备自动高可用。
