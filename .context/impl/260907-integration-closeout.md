# Integration closeout implementation

完成批准方案 `.context/proposals/260907-integration-closeout.md` 的三项修正，基线为 `b003b18b47a29e9699492d657328a192f930bb4e`，分支为 `feat/kapy-integration-closeout`。

- `process_start` 使用 `/bin/sh -c`，保留显式 child_env PATH。新增真实 Agent → MachineService → `/app/.venv/bin/kapy` Docker 回归，核对解析路径、帮助输出、退出码及模型收到的工具结果，模型使用 mock。
- agent durable recovery、skills service、gateway fixtures 使用 KAPY_DATABASE_URL / KAPY_VALKEY_URL 环境覆盖，保留旧默认地址、随机 schema/namespace、恢复重连和定向清理。
- 两个 agent acceptance 文件显式收窄 JsonValue，保留原行为断言。相对方案的实现细节：重复读取 archive parts 使用一个测试内 helper 集中断言；test_runner 的 caller fixture 注解补充已有 MachineCaller 协议，以接入真实服务测试。没有新增业务接口。

Elysia 在 Docker `kapy-v2-machine:dev`、项目 bridge `kapy-v2_default` 中完成检查，init、2 GiB/256 PID，只读挂载本 worktree src/tests/pyproject.toml，PYTHONPATH=/workspace/src；PG/Valkey 使用 postgres:5432、valkey:6379，KAPY_DOCKER_TEST=1。

- `uv run --project /app --no-sync pytest -q -p no:cacheprovider /workspace/tests/agent /workspace/tests/skills /workspace/tests/gateway /workspace/tests/agent/docker_manager_acceptance.py`：96 passed，20.99s。
- 全仓 `ruff check /workspace/src /workspace/tests`：PASS。
- 全仓 `pyrefly check --config /workspace/pyproject.toml`：0 errors，2 suppressed、15 existing warnings；原报告 79 errors 清零。
- Docker 隔离读取四处 fixture URL 环境表达式，缺省/覆盖均 PASS，无数据库连接。
- Docker 临时源码副本恢复旧 `-lc` 后，新 CLI 回归按预期失败，进程退出码 127；当前 `-c` 通过。
- `git diff --check`：PASS。

同一 persistent Elysia 完成实施；三个独立 Eden 组依次完成阶段 1–2、3–4、5，五阶段均 PASS，无遗留问题，无需返工。没有修改共享配置、State 测试、Token 或其他业务算法；Token 仍取 API 最新单响应 input_tokens + output_tokens，fresh usage 一次 sweep。未读主 .env，未调用真实模型或 Telegram，未重启或 flush 共享服务。

本次检查覆盖受影响的 96 项；全套标准 bridge 与真实递归 `scripts/check_recursive.py` 由总设计师独立验收。本 senior 提交实现及本记录，不合并 main。
