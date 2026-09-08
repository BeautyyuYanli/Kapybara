本轮以 `kapy_v2.md` 和用户最新澄清为准，基线 `a58ea93`，实现分支 `feat/session-model-protocols`，最终审查提交 `9451ec7cc80e33a74652568ad48735b124f27dd9`。方案位于 `.context/proposals/260908-session-model-protocols.md`，公共接口与使用说明位于 `docs/models.md`。

Provider 现在独立管理调用协议、端点和私有 key，支持 OpenAI Responses（默认）、OpenAI Chat Completions 和 Google AI Studio。新增 provider CRUD、显式 discover、持久目录读取，以及 model create/get/update API。每个 provider/name 对应稳定模型 UUID；重复探测更新观察值，保留 ID 和手动 defaults，不删除某一页缺少的模型。手工登记支持没有 models 接口的端点。变更与请求回执原子提交，revision 防止并发覆盖；普通查询和历史不返回 key。

Session 只保存 model_id 与显式预算覆盖，不再保存连接、协议或原始模型名称。有效预算按 session 覆盖 → 模型手动默认值 → 探测 metadata → 262144 context / 16384 output 计算。每次 Runner 调用冻结当前配置，provider/defaults 更新影响下一轮，不能改变正在运行的一轮。递归创建继承来源选择；换模型时未显式指定的覆盖重新继承新模型 defaults。CLI 和 Telegram 共用控制 API，发现失败形成安全回执，后续配置命令可以继续处理。

模型创建经可注入的 ModelBackendFactory，三种协议使用实际 Pydantic AI SDK。Responses 使用 store=false 和完整本地历史；通过公开 OpenAIModelProfile 保留合法 item IDs 与加密 reasoning，自定义模型别名也适用，不覆盖 SDK 私有 mapper。Google 保留 thought signatures。连接或模型身份变化只清理上下文投影中的供应商专有内容；仅预算变化保留签名，两者都清除旧 usage 的压缩依据。实际计数仍只取 API usage。安全、已知的配置失败通过通用且有界的 RunFailure 进入失败回执；未知 SDK 异常仍隐藏请求细节。

Agent 使用说明补齐 history 关系的完整 schema、受限 SQL、参数和查询示例，以及历史与输出游标的区别；现有 session 也收到当前能力说明，用户 instruction 和创建时 skill 快照保留。工具集合继续忠实于原设计：进程工具、read_media、wait 和显式注入的 ScriptTool；apply_patch 仍是普通插件，未重新加入 file_read/file_write 或虚构工具。前端继续通过 ControlAPI 接入，Runner 不依赖 Telegram。

相对最初的 session 配置草案，本轮按用户补充将协议、endpoint/key 迁至 provider，进一步加入稳定模型目录与独立默认值。删除旧服务端模型配置与 Runner 的原始模型名兼容分支。Google 依赖通过 uv 加入 pydantic-ai-slim[google,openai]；google-genai 要求 websockets<17，锁定依赖改用 16.x，未手改 uv.lock。审查另外发现 Unicode delta 经 JSON 转义可超过 State 16KiB 记录限额，将分块收窄到 1024 字符；这是传输大小约束，不是 token 估算。

按 cmd-proposal 完成一次简化审查后，复用同一 Elysia 实施及返修，三个独立 Eden 组完成 1–2、3–4、5 阶段，全部 PASS。审查修复了别名 encrypted reasoning 丢失、Telegram 发现失败堵塞队列、配置错误原因丢失、探测覆盖保存预算，以及无效测试和旧 CLI 帮助。补测覆盖同轮多次请求冻结、真实 SDK usage 的持久 checkpoint 恢复，以及子 session 换模型的预算重置。

最终独立非 root Docker 全仓测试在 `edda36d` 为 **361 passed、0 failed，142.13 秒**，仅一条 Google SDK 弃用警告；随后 `9451ec7` 只修 CLI docstring 与 README，实际 Docker `session update --help` 检查通过。Ruff/format、Pyrefly（0 errors）及 Git 差异检查通过。测试使用实际 PostgreSQL/Valkey、随机 schema/namespace、真实 SDK 和 mock provider/Telegram；未调用真实模型或 Telegram、未读取主 .env、未重启或 flush 共享服务。

代码交付时尚未部署；后续在用户明确授权下完成以下升级。旧部署模型环境变量不再作为运行时兜底。模型探测是显式、有界分页观察，不保证第三方端点提供窗口或完整目录；默认值与实际 API usage 仍是不同概念。

部署记录（2026-09-08）：先构建新镜像及基于 `a58ea93` 的回退镜像，确认无活动 run、输入、待发正文或机器进程，再备份本项目 schema。使用非 root 临时容器和真实控制 API 登记 Primary provider（OpenAI Responses），探测并持久保存 67 个模型。将原服务端的 1050000 context / 16384 output 设置保存为现用 gpt-5.6-luna、gpt-5.6-sol、gpt-6-astra 的用户默认值；10 个已有 session 与一个 Telegram route 改为对应稳定模型 ID，保留其他设置、模型选择、instruction/skill 快照及聊天绑定。没有重新创建用户 session。

真实部署验收发现旧机器代理方法白名单遗漏 `provider.*`；最小修复提交为 `23a06e3`，继续交由统一 ControlService 鉴权，不放宽 provider 权限。扩展现有真实 CLI/Unix/WS Docker 用例，验证管理员与 session 的目录读取，以及 session 修改共享默认值仍被拒绝。相关 19 项测试通过；最终类型收窄后再跑真实机器专项 1 passed，Ruff/format、Pyrefly 0 errors。此项为部署验收后的直接修复，不计入此前五阶段审查结论。

控制面和 daemon 已更新，最后的 Gateway 修复仅再次替换控制容器，daemon 自动重连。真实 Responses 模型（gpt-6-astra）→ State → Docker 进程工具验收通过：创建幂等、随机机器输出与最终回复及持久历史一致，9.11 秒完成，临时会话随后删除。最终 HTTP provider RPC 和真实 Unix socket → machine WS → Gateway 模型目录查询均成功。10 个原 session、11264 条历史及其摘要、Telegram poll offset/5 份 delivery cursor 与 projection 校验一致；当前无输入、run、inbox 或 cleanup 积压。Bot `yanli_test1_bot` 的 provider/model 命令已注册，没有发送额外 Telegram 测试消息。

运行源码校验与仓库一致；两应用使用 UID10001，daemon 的 CapEff=0、NoNewPrivs=1。PostgreSQL、Valkey 和 network 容器未重建，机器持久卷保留。模型密钥已写入 provider 私有配置，部署临时凭据副本完成后删除，未修改或提交 .env。
