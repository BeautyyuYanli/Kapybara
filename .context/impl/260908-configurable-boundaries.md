本轮以 `kapy_v2.md` 和用户最新澄清为准，基线 `ca22ab7`，最终审查提交 `79241bde00e129f9a6bace848a29a6cafe34afda`。未发现需要推翻原始设计的内生矛盾；State、Execution、Skills 的主要分工与持久化链路符合构想。具体证据见 `.context/reviews/260908-kapy-v2-alignment.md`。

已修正四类偏离：模型构造及错误分类移入可注入的 ModelBackend，首个适配器使用 OpenAI-compatible 接口；前端通过 ControlAPI 接入，Telegram 自有迁移与投递数据；删除模型侧 file_read/file_write；apply_patch 改为应用装配时注入的普通 ScriptTool，Runner 和 MachineTools 不再自动注册或按名字特判。底层字节传输、媒体、Skills 和脚本 stdin 保留。

模型端点、模型名、前端列表和工具插件均可配置，也可从 Python 装配入口注入。原 OPENAI_* 环境变量保留为配置别名。补齐 agent 的递归 CLI、历史/Skills、机器选择、PTY 游标和等待说明；旧 session 的 instruction/skill 快照保持原样，新基础指令用于新 session。

按 cmd-proposal 完成一次简化审查后，复用一位 Elysia 实施，三组独立 Eden 完成五阶段审查，全部 PASS。返工删除了失去用途的部分文件读取逻辑，修正 Telegram 旧创建确认重试导致的回复越序，并去掉误导性测试及补齐 integration 标记。没有增加供应商协议、动态插件加载、调度框架或新的业务接口层级。

最终非 root Docker 全仓测试：327 passed，136.59 秒，零跳过。Ruff check/format、Pyrefly（0 errors，保留既有 warnings）、Git 差异和 Compose 配置检查通过。覆盖真实 PostgreSQL、Valkey、Execution、apply_patch，以及模型适配、前端隔离/恢复、插件关闭/替换、旧工具恢复和 Telegram 持久接续；模型和 Telegram API 使用模拟端点。另独立验证注入的模型 HTTP client 在 Gateway 退出后仍可用，由调用方关闭。

未新增依赖或手改生成资源，未修改真实凭据。API usage 计数和既有数据语义不变；Telegram 正式 ACK 丢失仍可能重复分段。

部署结果（2026-09-08）：审查报告随 `edda896` 快进合入 main，构建并仅更新 Compose control/daemon。更新前没有活动输入、进程或待发回复；更新后控制 RPC 与实际 Unix socket → machine WS → Gateway 的只读 session.list 均通过。四个 session 的 instruction/skill 快照摘要一致，69 份请求记录、Telegram offset 和四份 version 1 投递状态保留，无积压或 blocked_error；启动日志无错误和警告。两应用均使用 UID 10001，执行机 CapEff=0、NoNewPrivs=1。PostgreSQL、Valkey、网络和开发容器未重建。本次未额外发送 Telegram 测试消息或调用真实模型。
