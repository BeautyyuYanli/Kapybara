# PTY Ctrl-C 集成测试观察修复

分支 `feat/kapy-execution-complete`，修复提交 `d6bce3410a4e806ff238a2b0e5d72dff246eb863`。仅修改 `tests/execution/test_processes.py`，无生产代码或公共契约变更。

Gateway 集成发现 Ctrl-C 后测试的 process.wait 缺省 cursor=0，会重新观察已静默的历史输出，合法地立即返回 quiet/running。专属 Docker 单跑复现原失败（约 0.60 秒）。quiet 表示输出静默，不保证进程终止。

测试现在从上次返回的 next 开始，在共用三秒总期限内推进 cursor，直到终态。保留真实 Ctrl-C 写入、exited 和非零退出码断言；没有固定 sleep、吞掉异常或重置期限。Ctrl-C 无效仍会超时失败。

同一 persistent Elysia 完成定向返工。因仅修改测试，回到第3–4组时创建新上下文 Eden，阶段3测试卫生 PASS、阶段4充分性 PASS；新第5组 Eden 文档 PASS，无需更改生产文档。原生产阶段1–2结论不受影响。

全部运行验证仅在规定的 kapy-v2-machine:dev Docker 中进行：修复后单例通过；预先收集10份同一PTY用例得到10 passed/150 deselected（4.78秒）；Execution/RPC全套100 passed（10.30秒）。Ruff、diff和该测试Pyrefly通过。原计划重复node ID被pytest去重的运行仅计作单例，不计入重复次数。

Gateway 可单独 cherry-pick 修复提交，无需新接口或业务改动。本报告为后续独立文档提交。
