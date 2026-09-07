Intelligence 已完成批准范围，cmd-impl 五阶段全部通过。

- Worktree：`/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/a88a199c-41c1-49d6-ad09-2d5b87f80b12`
- Branch：`feat/kapy-agentskills`
- 实现与文档审查提交：`de2227ecc833d0b258606a09a8b2b6a56fa1e8d4`
- 使用说明：`docs/agent-skills.md`；批准输入：`.context/proposals/260907-agent-skills.md`。

已交付真实 `Runner`、`Runner.initial_state`、`AgentPayloadStore`、`SkillService` 与归档 helpers。Runner 直接使用 State 类型，Gateway 注入 MachineCaller、HTTP client、payload store 和 authorize_wait；State 控制生命周期及 session 内串行性。没有第二套 session/event 系统或兼容 RPC。

模型循环采用已安装 Pydantic AI 2.40 的流式 API。压缩只使用单条 response 的实际 input_tokens + output_tokens，每份 fresh usage 最多一次 sweep；最新约 10% 按完整交互块保留。首次未知 usage 不估算，只有明确 context-too-long 允许有限额外重试。未增加 tiktoken 或媒体 token reserve。

媒体先保存不可变 bytes，再提交 session_id + SHA256 引用；超过 2 MiB 的上下文外置，单 payload 上限 64 MiB。恢复从 PostgreSQL hydration，媒体被模型拒绝后以同一 call ID 的文字结果继续。PayloadStore 借用 Gateway pool，无 State FK；Gateway 在 State 停止并删除 session 后持久重试媒体清理。

机器工具使用既定 process.start/wait 和 file transfer RPC。脚本通过文件传输与固定 argv 重定向 stdin。apply_patch 两平台资源由生成器按固定源和 hash 生成，包含原始许可；安装及执行已通过 Docker 中的真实 Execution manager 验证。Skills 提供 PostgreSQL CRUD、幂等回执、revision 冲突、description catalog/substring、SKILL.md、原 ZIP 下载及安全 pack/extract，保留批准的 16 MiB ZIP 等限制。

相对批准方案，公开签名与资源归属没有偏离。审查促成的主要简化和修复是：释放无用解压内容、避免元数据查询读取 ZIP；取消时等待验证线程收束并清理 transfer；恢复已完成的最终输出和 wait，按实际 Pydantic exhaustive 语义区分函数工具重试与输出工具校验重试。终结状态仍位于既有 opaque RunnerState，未扩展 State 契约。

| 阶段 | 结果 | 完成内容 |
| --- | --- | --- |
| 1 代码 hygiene | PASS | 删除大对象保留和多余数据库投影；返工后复审通过。 |
| 2 需求完整性 | PASS | 取消恢复、同批工具与 wait 语义的反馈全部闭环。 |
| 3 测试 hygiene | PASS | 删除无效断言，修正夸大测试范围的名称；新增测试复审通过。 |
| 4 测试充分性 | PASS | 补齐真实 manager、提交时序、PG 恢复、并发隔离、usage、事务和归档失败证据。 |
| 5 文档充分性 | PASS | 更新可复现验证入口及归档 helper 路径契约。 |

全过程复用同一 Elysia；三个 Eden 组分别负责 1–2、3–4、5，每组使用独立新上下文，并在组内复用处理返工。

验证结果：`uv sync --locked` 完成；`uv run pytest -q tests/agent tests/skills` 为 **59 passed**，最后仅调整数据库测试夹具后，受影响测试重跑 **4 passed**。`uv run python tests/agent/run_docker_acceptance.py` 为 **2 passed**。本域 Ruff、pyrefly 与 diff 检查通过。Docker launcher 使用只读的 Execution `27a34ddd9740b570c4122ac9e3582ca0eecd3ede` 快照和 `kapy-v2-machine:dev`，所有真实机器操作留在隔离容器内；PostgreSQL 测试每次使用独立 schema。

实测边界：Docker 使用真实 ExecutionStore/MachineService，模型和 State 使用测试替身；Skills、PayloadStore 使用真实 PostgreSQL。未执行真实模型、Telegram、真实 State 服务或 daemon 网络传输的全产品端到端验收；双平台资源 hash 校验不等于双平台执行验证。全产品装配验收与合并仍由总设计师负责。

共享 pyproject、uv.lock、compose、README 未被本域实现修改；未读取或提交 .env，未发送真实 Telegram 消息，未合并到 main。无新增依赖或待裁决公共接口。
