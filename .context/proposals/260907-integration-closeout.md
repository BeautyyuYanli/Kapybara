# Kapy v2 integration closeout

基线为 main `b003b18b47a29e9699492d657328a192f930bb4e`，工作分支 `feat/kapy-integration-closeout`。本方案仅修正三项已复现的集成缺陷。

1. `src/kapy/agent/machine.py` 的 `process_start` 将 shell argv 改为 `["/bin/sh", "-c", command]`，保留 Execution 显式传入的 child_env PATH。沿用现有 process.start RPC、进程标识及生命周期。在 `tests/agent/docker_manager_acceptance.py` 加入真实 MachineService 回归：通过 agent process_start 路径，在 child_env 中明确设置包含已安装 kapy 的 venv bin 的 PATH，实际执行 `command -v kapy` 和 `kapy --help`，核对解析路径、输出和退出码，完成服务及进程清理；模型响应使用 mock。
2. `tests/agent/test_durable_recovery.py`、`tests/skills/test_service.py`、`tests/gateway/conftest.py` 的连接地址统一采用 `os.environ.get("KAPY_DATABASE_URL", 原PG地址)` / `os.environ.get("KAPY_VALKEY_URL", 原Valkey地址)`。缺省分别保留 `postgresql://kapy:kapy-local@127.0.0.1:55432/kapy` 和 `redis://127.0.0.1:56379/0`。同文件重复使用模块级常量；相关组合测试/helpers复用这些值，建连、迁移、恢复重连和清理使用相同地址。保留每次随机 schema/namespace 及定向清理，不改 State 测试。
3. `tests/agent/docker_manager_acceptance.py`、`tests/agent/test_acceptance.py` 在遍历、索引 State JsonValue 前，以局部 `isinstance` 断言明确 list/dict/str 等结构，并补足必要 fixture 注解。断言必须检查数据形状，不能过滤掉错误数据后空集合通过；保留现有行为断言，不用 Any/cast 或 ignore 掩盖这些错误。

公开 Python/RPC 签名和持久化结构均不变；不修改共享 pyproject.toml、uv.lock、compose.yaml、README.md。Token 继续使用 API 最新单响应 input_tokens + output_tokens，每份 fresh usage 只允许一次 sweep；不改压缩、RPC、State、Runner、CLI 业务算法或已修好的 cursor 行工厂。

按委托，实施检查全部在 `kapy-v2-machine:dev` Docker 中运行：网络 `kapy-v2_default`，`KAPY_DOCKER_TEST=1`，只读挂载本 worktree 的 src/tests，`PYTHONPATH=/workspace/src`，init、2 GiB/256 PID；连接 `postgresql://kapy:kapy-local@postgres:5432/kapy` 与 `redis://valkey:6379/0`。用 uv 运行相关 agent/skills/gateway 测试、Docker manager 验收和全仓 Ruff/pyrefly，要求本次 79 errors 消除且全仓检查通过；环境缺省检查不依赖宿主机服务。测试只清理自身资源，不重启或 flush 共享服务，不读主 .env、不调用真实模型或 Telegram。真实递归由总设计师运行 `scripts/check_recursive.py` 验收。

本轮仅提交方案；收到总设计师明确实施批准后，执行 cmd-impl，由本 senior 管理 persistent Elysia + Eden 的 1–2、3–4、5 三组阶段，提交实现及检查结果，由总设计师验收合并。
