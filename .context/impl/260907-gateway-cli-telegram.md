Gateway / CLI / Telegram 已完成，并通过 cmd-impl 五阶段审查；全程复用同一 Elysia，Eden 按 1–2、3–4、5 分组审查与复核。

分支：`feat/kapycli`。审查代码快照：`9ff3086ebcafa218c6ddbfd73361afce96101803`。

Worktree：`/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/0f9cff83-dff7-4881-b5a1-a6b1e761dd27`。

实现包括 FastAPI 生命周期装配、机器认证与双向代理、Gateway 授权和持久请求恢复、State session/output/wait/history 路由、Skills 分块传输与 CAS、CLI，以及 Telegram 持久 inbox、topic 配置、输出投影和重试。已集成 State、Runner、Skills、AgentPayloadStore、RPC、Execution 的真实导出，没有生产 stub。

相对方案的主要收敛：

- 删除重复回执提交、手写递归序列化、重复发送偏移和无效管理员选项。
- 固定创建快照及技能传输机器；确定拒绝保存错误回执，传输结果未知保留恢复能力。
- 关联撤销保留 provision 台账，仅最终删除调用永久 `session.release`；媒体清理在 State 停止 runner 后执行。
- 采用 Execution 完成版，移除过时 cgroup 配置及其旧测试；token 计数只依赖 API usage。

验证记录：

- `kapy-v2-machine:dev` 内运行 `pytest -q -p no:cacheprovider tests/gateway tests/cli tests/execution tests/rpc`：**144 passed，28.41 秒**。容器使用 `KAPY_DOCKER_TEST=1`、只读源码/测试挂载、1 GiB 内存与 128 PID 限额；数据库逐次使用独立 schema/namespace。
- 真实组合覆盖 CLI server、Unix→WebSocket→Gateway 代理、身份隔离、多块技能上传/CAS/下载、重连和最终清理。上游 PTY 观察失败已由 Execution owner 在 `d6bce34` 修复并合入，重跑通过。
- Pyrefly 全量 `src tests` 及后续变更检查：**0 errors**；自有源码/测试 Ruff、format、diff 检查通过。
- `uvx --python 3.14 --from . kapy --help`：本地包构建及入口调用通过；`session update --help` 已验证完整替换说明。

Telegram 和模型 HTTP 全部使用 mock；未读取主目录 `.env`，未发送真实 Telegram。无需新增共享依赖或配置改动；未自行合并 main。Telegram 远端发送成功但本地确认前崩溃仍可能重复，保留方案中的 at-least-once 边界。
