# Execution/RPC 完整模块交付

分支 `feat/kapy-execution-complete`；最终代码提交 `8cbbfd8f3b8f43aa6fbfca69dca029eea88c80f5`。本报告由后续独立文档提交加入。接管从 main `8d42e77` 开始，合并指定 `fdf4cb642260d008f1d524a8d714f2c0482b9fd1` 为 `078ab79`，未合入 `9ec91c8` 或旧分支后续代码。实现依据已批准 `c7d2d3f` 方案及普通 stdio 持续收集的补充澄清。

## 实现与差异

- `config.py` 与公共导出：真实 `DaemonConfig`、`run_daemon(config, *, stop=None)`；early commit `549298b` 已通知 Gateway、Intelligence。完整 Gateway ws/wss URL 原样使用，独立 machine Bearer 和 `kapy.jsonrpc.v1`，没有 cgroup 字段或 systemd/delegation 探测。
- `processes.py`：stdio stdout/stderr 完整磁盘 spool、每路 64 KiB 分页；PTY 8192 raw-byte 尾窗、cursor/truncated、输入、实际窗口 resize 与 Ctrl-C；16 活动进程上限。普通 leader 退出后仍持续收集同组后代输出，不使用任意短收束期限。quiet/timeout 只结束观察。
- `_pty_exec.py` 仅用于 exec 前建立 controlling terminal，不涉及 cgroup 或环境探测。kill 使用普通 process group；kill/shutdown/故障路径最多等待两秒排空，强制收束时明确标记输出不完整。
- `store.py` 升级已有 SQLite 到 schema v2，复用锁、worker 与 session 表。进程以 session/process ID 和参数摘要幂等；终态 spool 在声明完整前 fsync，恢复核对文件长度；未终态恢复为 lost、不重跑。旧进程清理仅在 boot ID 与 leader starttime 匹配时进行。release 保留 tombstone，启动续清中断的删除。
- `daemon.py` 装配真实 store/file/process/HTTP/Unix proxy/WS。manager 持有域任务，观察连接断开不取消已接受工作。session ensure 刷新 token；release 撤销身份、拒绝新工作、结束进程与 transfer，再删除自有目录。后台 release 有独立上限并复用同 session 任务。启动获取与注册、取消退出均保护资源收束。
- 重连采用 1–30 秒带 jitter 退避；认证/协议错误明确失败。可选 idle 断线定时重连，本地 proxy 唤醒；caller auth 与目标 params 分离，未知副作用不自动重试。子进程只使用显式安全环境和四个 KAPY 身份字段，不复制 parent secrets。
- 保留 fdf4cb6 文件传输和 call_local_proxy；冻结 RPC 没有扩展，stdin 插件继续使用文件传输与固定 shell 重定向。共享 pyproject/uv.lock/Compose/README 未修改，无依赖变更。未修改生成文件。

真实联调入口为 `kapy.execution.daemon.MachineService(store, http_client=client, child_env=...)`：先进入 ExecutionStore 和 HTTP client，再 initialize，调用 handle(method, params)，最后 aclose 并退出 client/store。先 session.ensure 再启动工作；完整说明在 `src/kapy/execution/README.md`。真实入口与首轮提交 `08f9a90` 已通知两位 senior，无 production stub。

## 检查结果

所有真实 process/PTY/file/daemon 检查在专属 `kapy-v2-machine:dev` Docker 内完成，使用 `--rm --init --network none --memory 1g --pids-limit 128`，只读挂载 src/tests/pyproject，`PYTHONPATH=/workspace/src`、工作目录 `/workspace`；URL loopback、XDG/tmp 均在容器内。不挂 Docker socket/.env，不使用 privileged/hostPID，不执行宿主进程实验。

- 最终代码 `8cbbfd8`，Eden 独立运行 `/app/.venv/bin/pytest -q -p no:cacheprovider tests/execution tests/rpc`：**100 passed in 10.27s**，包含已有 64 MiB WS/URL 文件传输回归。
- 覆盖：64 MiB stdio 分页哈希、stderr 精确内容/EOF、leader 退出后延迟输出、真实后代退出、PTY 输入/Ctrl-C/实际尺寸、尾窗与重启 cursor、16 并发 admission/cleanup、身份与 token 刷新、幂等/tombstone、数据库连续失败、fsync 失败、缺失/缩短 spool、恢复 boot 身份、中断 release、真实 WS reconnect/idle/proxy、AnyIO 启动及运行取消。
- Ruff：`uvx ruff check src/kapy/execution src/kapy/rpc tests/execution tests/rpc` 通过；`git diff --check` 通过。
- Docker Pyrefly 全域 `src/kapy/execution src/kapy/rpc tests/execution tests/rpc`：0 errors，工具报告 3 warnings not shown，未展开类别，不宣称 warning-free。最终变更范围 `src/kapy/execution tests/execution`：0 errors。

## cmd-impl 五阶段

唯一 persistent Elysia 完成全部实现和返工。三组 Eden 均以独立新上下文建立，组内复用；Mei 审核反馈后再交 Elysia，不自行修改域代码。

| 阶段 | 结果与返工 |
| --- | --- |
| 1 代码/逻辑卫生 | 最终 PASS。修复启动后数据库连续失败导致 done 未设置、关闭永久等待；后续生产返工均重新从本阶段开始。 |
| 2 需求完整性 | 最终 PASS（5970d18）。补齐 AnyIO 取消与启动注册窗口、跨 boot 身份、spool 持久化与恢复完整性、中断 release、致命 WS 二进制帧、release 上限、退避范围。 |
| 3 测试卫生 | 最终 PASS（8cbbfd8）。删除绑定内部 save 次数的断言，保留真实行为验证；第四阶段返工后再次通过。 |
| 4 测试充分性 | 最终 PASS（8cbbfd8）。补齐后代实际退出、终端实际尺寸、stderr 内容与 EOF、PTY 尾窗重启分页；独立 Docker 全套 100 passed。 |
| 5 文档 | PASS（8cbbfd8），无返工。确认真实入口、正常输出与清理边界、环境、恢复、proxy/idle 说明一致；未改 proposal。 |

## 限制与交接

进程组清理不保证杀尽 setsid/double-fork 逃逸；lost 不接管旧 PTY、不自动重跑。正常运行可能因仍存活同组后代继续等待，调用者可显式 kill。逻辑 session 隔离不是同 UID 文件安全沙箱。跨 daemon 重启不续传未完成 transfer，沿用基线 failed 语义。

本轮验证了 64 MiB stdio 完整性及 16 并发状态/清理，但没有单独测量两者的峰值 RSS 或分项吞吐耗时；文件传输既有 RSS 测量不能替代它们。模块交付不宣称已完成跨域产品验收或 Intelligence apply_patch 联调，这些由各域和总设计师用真实入口继续组合。未合并到 main。
