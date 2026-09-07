State 已完成批准范围内的实现，并通过 cmd-impl 全部五阶段审查。实现基于批准的 proposal 与 main 的统一接口裁决；没有待批准的 State 接口决策。

工作目录：`/home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/37703573-1ccd-4f8b-a137-ba52ca9e8e60`。分支：`feat/kapy`。本报告对应实现提交：`6cdbda21f76eaeefce5beaf12f0898d1c5a5a032`。报告随后单独提交，交付消息提供最终 SHA；由总设计师集成，不自行合并 main。

实现位于 `src/kapy/state/`，测试位于 `tests/state/`，使用说明和事件 JSON 契约位于 `docs/state.md`。公共 DTO、RunContext、RunResult、CheckpointWrite 和 SessionRunner 已在早期提交 `4b423490884ef824c0dae652cb82073a0f7759bb` 发布并通知依赖方。后续已批准的 Completion、SubmissionStatus、HistoryExportPage 及对应 service 方法在 `5054bdf9b1137d13fb60ecb79dae3b865c8e082c` 落地。

PostgreSQL 是权威存储。迁移创建指定 schema，按顺序执行 SQL 并校验 checksum；SessionService 自持 psycopg pool、租约连接、真实 Valkey client/PubSub 和后台任务。单个 schema 只允许一个 control process；epoch 和 run/attempt 校验阻止失去所有权的写入。短写事务通过 schema 内的控制行串行提交，模型执行在事务外进行，不同 session 可并发，同一 session 的 runner 不重叠。

输入、steer/queue、输出、完整消息、checkpoint、订阅、事件和请求回执均持久化。输入先 reserved，只有 checkpoint 事务才能确认 consumed；恢复继续原 run_id 并递增 State attempt。完成事务同时写最终状态、waiting、订阅替换、backlog 投递和 completion。事件支持当前订阅者广播、无订阅者 backlog、自身生产者排除、多次唤醒和递归完成。关闭保留恢复所需状态；删除先持久化 intent、停止 runner，再完成回执并物理删除 session 记录。Valkey 仅发送唤醒 hint，丢失全部 hint 后仍由 PostgreSQL 周期扫描找回工作。

Gateway 可使用 session CRUD、input、output、event、history/search/query、wait_submission 和 export_history。写操作使用调用方 UUID request_id；update/delete 重放返回第一次持久结果。wait_submission 是可重复、非消费的观察，删除后的 completion 仍可读取。export_history 在固定 snapshot 内分页；它不冻结运行中的 session。输出和历史共用有序记录序列，但历史只包含 input/model_request/model_response/final/waiting/error；完整 delta/interrupted 回放必须使用 read_output。

历史 SQL 经完整 AST 白名单重新生成，只能访问授权 session 的 MATERIALIZED 子视图；join、子查询和函数不能绕过过滤。读事务具有只读、超时和受控 search_path，禁止任意物理表、未知函数及写语句。substring 使用参数化字面匹配；全文检索采用 PostgreSQL simple GIN，辅以 Unicode 归一化及 CJK token/字面校验。读取直接组装 dataclass，不经过 ORM/Pydantic 校验。

相对原方案的主要调整如下：

- 按总设计师批准，将页面上限统一为 512 KiB，计算实际 JSON 编码、转义、分页 envelope、最长 cursor 及终页 false；保留单项限制。采用 server cursor，避免先将大量结果加载到内存。
- 加入已批准的 update/delete UUID 幂等、非消费 receipt 观察和固定 snapshot 导出。没有加入过时草案中的 RequestKey、AuthorizationScope、owner/parent/grant DTO 或第二套 RPC。
- 一次 run 可以经多次 steer 消费大量请求，因此 completion 的 request_ids 每批最多 64 个。超过 64 个时，同一 run 的默认频道收到多条终态通知；每个请求自己的 receipt 和等待频道通知仍完整、同事务提交。这是审查发现超限故障后必须记录的行为调整。
- 删除了未使用的生命周期标志、重复 AST 校验和不可达删除补偿分支；没有新增通用 blob 服务、频道实体表或定时 delta 批处理层。
- 测试支持 KAPY_DATABASE_URL/KAPY_VALKEY_URL 环境覆盖，恢复子进程使用同一有效地址；不加载 .env。

cmd-impl 使用一个 persistent Elysia 完成实现和全部返工，并按要求分组创建无继承上下文的 Eden。第 1/2 阶段最终在 `2c0c9ac` 通过；第 3/4 阶段在 `fd11ab9` 通过；第 5 阶段在 `dae983e` 通过。补齐环境配置后，以新 Eden 重新访问第 3/4 组及第 5 组，均在 `6cdbda2` 通过，生产实现未变。

审查修复并验证了三类生产问题：13,200 个已接收请求删除时的 completion 聚合超限；runner 自身抛出 NotFound/ServiceUnavailable 被误当作生命周期终止；终页 JSON false 与 cursor 长度造成的单字节越界。测试审查还修正了“完成回执观察之前缺少持久状态基线”和“将失败回执计作成功完成”的证据缺口。临时故障注入确认修订后的测试会拒绝这两类错误，注入代码没有提交。

验证结果：

- `uv run ruff check src/kapy/state tests/state` 通过；`uv run pyrefly check src/kapy/state tests/state` 为 0 errors。
- `uv run pytest -q -s tests/state` 在 `dae983e` 完整通过：56 passed，88.61 秒。此后仅测试地址配置及文档改变；`6cdbda2` 再次通过静态检查，以及显式 localhost 覆盖下的 CRUD 和真实 kill/checkpoint 恢复两项测试，0.46 秒。
- 真实 PostgreSQL/Valkey 的 100 session × 20 input 负载：accepted、成功 completed、replayed 均为 2000，失败为 0，100 个 session 顺序正确；16.028 秒，124.78 inputs/s，观察到最多 4 个并行 fake runner。
- 100 listener 广播：100 个不同 listener 获得并消费事件；投递 0.492 秒，全部完成 1.179 秒。其他测试覆盖早到 backlog、self exclusion、取消订阅、递归完成、重复唤醒和重启。
- 13,200 请求删除回归和独立审查探测确认所有 receipt、原 waiting 频道通知完整。SQL 恶意访问、跨 session cursor、JSON 页边界、事务回滚、epoch fencing、丢失 hint 与真实控制子进程崩溃恢复均有覆盖。

每个集成测试使用独立随机 schema/Valkey namespace，清理仅限其创建的 schema。未重启或 flush 共用服务，未读取或提交主目录 .env，未发送真实 Telegram 消息。共享 pyproject.toml、uv.lock、compose.yaml 和 README.md 未修改，无新增依赖申请。

对其他 owner 的要求保持批准契约：Intelligence 实现 SessionRunner(ctx)，在 checkpoint 前保存不可变媒体内容，负责 opaque reference 的 codec/hydration 和不确定外部副作用的恢复；模型重试标识放既有 data 字段，不改变 State attempt。Gateway 负责认证、授权、请求归属及删除 outbox，装配迁移和资源生命周期；媒体清理由 Intelligence 提供入口，Gateway 在 State runner 停止/删除后调用。完整 Telegram 输出投影使用 read_output，UI completion 使用 wait_submission，不消费 agent 订阅队列。

已知边界：短事务仍会随广播 audience/backlog 增大而延迟其他写入；没有承诺吞吐 SLA。SQL 是有界 SELECT 子集，单条超大 SQL 结果在取回后拒绝；全文检索不承诺词形还原或完整语言分词。记录和 pending events 没有隐式 TTL；固定快照不保留被删除记录。State 不保证外部命令 exactly-once，也不负责媒体 blob、机器清理或 ACL。上述负载是本机真实存储加 fake runner 的模块证据，完整产品端到端验收由总设计师负责。
