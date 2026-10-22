# Kapy v2

当前开发栈围绕 `src/kapy` 的 Python 模块：本地进程与 PTY、文件传输、
Temporal standalone Activity Runner，以及基于 Valkey Pub/Sub 的实时输出和历史回放。
HTTP 和 Telegram 作为独立接口进程，直接调用这些服务。HTTP 同时挂载配置前端。

主实现位于 `src/kapy/`，测试位于 `tests/`，模块直接通过 `kapy.*` 导入。通用 layer 定义、注册与选择位于独立包 [pydantic-ai-layer-protocol](packages/pydantic-ai-layer-protocol/README.md)，
Kapy 使用该包并保留自己的 shell layer 与会话生命周期。
独立引入的 shellctl 位于 [packages/shellctl](packages/shellctl/README.md)，包含
Go 服务端和可直接 `import shellctl` 的 Python SDK。其上游源码由
`scripts/vendor_shellctl.py` 按固定 commit 同步，勿手工编辑脚本管理的路径。
反向 HTTP 隧道库 [Holt](packages/holt/README.md) 位于 `packages/holt/`，
通过 Git 子模块引入 [BeautyyuYanli/holt](https://github.com/BeautyyuYanli/holt) fork，
版本由本仓库记录的子模块 commit 固定。
早期实现及其测试、脚本原样归档在 `prototype/`，不参与当前安装、构建和默认检查。
`kapy` 命令提供 `interface` 与 `db` 两个入口；旧控制面 CLI 随原型一起归档。

独立 MCP Gateway 位于 [packages/mcp_gateway](packages/mcp_gateway/README.md)，
使用 FastMCP 聚合上游，提供 inline stdio 和 HTTP / JWE 两种入口。
[MCPs layer](src/kapy/layers/README.md#mcps-layer) 从 session 配置创建独立 stdio
Gateway，并供应原生 MCPToolset；这条私有管道不使用 JWE。普通 Agent 由 SDK 管理
进程，生命周期为一次 run；standalone Activity 内的整个 Agent run 使用同一 SDK scope。
也可通过 `uv run --locked --package kapy-mcp-gateway mcp-gateway` 单独运行。

## 本地启动

需要 Docker Compose。镜像提供 Python 3.14 和锁定版本的开发依赖。
首次配置可以复制 `.env.example` 为 `.env`；已有 `.env` 直接复用。
填写 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。
镜像构建时使用 npm 生成前端产物，无需手动维护 `dist`。

```sh
git submodule update --init --recursive
docker compose build runtime
docker compose up -d --wait postgres valkey temporal-namespace
# 首次启动：核心和 Telegram 数据库分别升级，serve 不自动迁移。
docker compose run --rm runtime kapy db upgrade
docker compose run --rm telegram kapy interface telegram db upgrade
docker compose up -d runner-worker runtime telegram
# 可选：启动本地数据库和 Temporal 面板。
docker compose up -d drizzle-gateway temporal-ui
docker compose ps
```

| 服务 | 用途 | 宿主机入口 |
| --- | --- | --- |
| `postgres` | durable runner_state、history、session 配置与当前写入身份 | `127.0.0.1:55432` |
| `drizzle-gateway` | PostgreSQL 网页面板，浏览编辑数据和执行 SQL | `127.0.0.1:5050` |
| `valkey` | 按 session 广播临时输出；无离线消息、TTL 或持久化 | `127.0.0.1:56379` |
| `temporal` | 持久化 standalone Activity 调度；数据保存在 PostgreSQL 的独立数据库 | `127.0.0.1:7233` |
| `temporal-ui` | Activity 执行状态与服务数据网页面板 | `127.0.0.1:8080` |
| `runner-worker` | 执行完整 Agent Activity，持有模型及输出资源 | 无入站端口 |
| `runtime` | 非 root 的 HTTP 接口和配置前端 | `0.0.0.0:8000`，前端 `/app/` |
| `telegram` | 独立 Telegram session 接口，通过 Bot API 长轮询 | 无入站端口 |

Drizzle Gateway 在 `http://localhost:5050/`，管理员密码默认为 `kapy-local`，
可通过 `KAPY_DRIZZLE_MASTERPASS` 修改。首次使用时，在面板添加 PostgreSQL
连接：主机 `postgres`、端口 `5432`、数据库 `kapy`、用户名 `kapy`、
密码 `kapy-local`。进入连接后，选择 `KAPY_DATABASE_SCHEMA` 对应的 schema
查看业务表；`public` 不一定包含业务数据。连接和面板配置保存在
`drizzle-data` 命名卷。
该面板直接连接现有数据库，无需在项目中引入 Drizzle ORM。
Temporal UI 在 `http://localhost:8080/`，直接连接 `temporal:7233`，默认打开
`KAPY_TEMPORAL_NAMESPACE` 指定的 namespace（未设置时为 `default`）。
两个面板默认监听宿主机回环地址。需要局域网访问时，在 `.env` 中将
`KAPY_PANEL_HOST` 设为宿主机的局域网 IP，重新执行
`docker compose up -d drizzle-gateway temporal-ui`，通过该 IP 和对应端口访问。

HTTP 直接启动入口和 Telegram 普通文本均提交到 Temporal，需同时运行
`runner-worker` 才会执行；接口进程不自带 Worker。详见
[Temporal standalone Activity Runner 与 Worker](src/kapy/runner/README.md)。

源码、测试和脚本挂载为只读；更新依赖或前端后需要重新构建。
PostgreSQL、runtime 和 Telegram 状态目录分别保存在 `postgres-data`、
`runtime-data`、`telegram-data` 命名卷。ProcessManager 默认使用
`/var/lib/kapy/state/kapy` 保存 SQLite 和进程输出；Telegram 使用独立卷中的
`/var/lib/kapy/state/kapy/plugins/telegram/telegram.sqlite3`。

Compose 从 `.env` 读取 `KAPY_DATABASE_SCHEMA` 和 `KAPY_VALKEY_NAMESPACE`。
容器内数据库和 Valkey URL
使用 Compose 服务名；`.env.example` 的 URL 用于宿主机直接调用模块。
Telegram 复用 `.env` 中的 bot token 和 chat ID，
也可用 `KAPY_TELEGRAM_ALLOWED_CHAT_IDS` JSON 列表配置多个 chat。
启动不创建 provider 或 model。配置前端在 `http://localhost:8000/app/`。
HTTP API、WebSocket 和前端直接访问，无需登录或访问 token。
创建模型后在 Telegram 发送 `/model <provider UUID> <model name>`，同时更新当前
聊天/话题的 session 模型并保存 bot 默认选择，后续新会话也使用它；选择重启后保留。
`/model` 不带参数显示当前默认值。尚未选择模型时 bot 仍可启动，并提示配置命令。
`KAPY_TELEGRAM_SESSION_TEMPLATE` 配置新会话模板，可包含固定的 `layers` 配置；
已保存的模型选择仅覆盖模板中的 provider/model，其余会话选项继续保留。
可用 `KAPY_HTTP_PORT` / `KAPY_POSTGRES_PORT` / `KAPY_VALKEY_PORT` /
`KAPY_TEMPORAL_PORT` / `KAPY_DRIZZLE_PORT` / `KAPY_TEMPORAL_UI_PORT`
更改映射端口。

## 验证

主实现测试使用真实 PostgreSQL、Valkey、Temporal、子进程、PTY 和本地 HTTP 服务；
模型行为使用 SDK 的确定性测试模型，不调用外部模型 API。
进程和文件测试要求在容器中运行，避免在宿主机跳过。

```sh
docker compose exec -T runtime python -m pytest -q -p no:cacheprovider tests packages/pydantic-ai-layer-protocol/tests
```

测试覆盖 standalone Activity 的竞争准入、单次执行与取消、模型/工具资源关闭、
runner snapshot 与 layer State 原子保存，以及固定 history 和实时输出的断线补读。
模型协议通过本地 HTTP 服务验证，不调用外部模型 API。

## 模块入口

- [进程管理 API](src/kapy/processes/README.md)
- [文件传输](src/kapy/file_transfer.py)
- [完整消息历史](src/kapy/agent_history/README.md)
- [实时输出契约](src/kapy/agent_output/README.md)
- [Temporal standalone Activity Runner 与 Worker](src/kapy/runner/README.md)
- [Valkey 输出服务](src/kapy/agent_output/service.py)
- [SessionService：生产侧与历史回放](src/kapy/control/sessions/service.py)
- [独立进程接口](src/kapy/interfaces/README.md)
- [核心与接口数据库迁移](src/kapy/database/README.md)
- [控制面 FastAPI HTTP / WebSocket API](src/kapy/interfaces/http/README.md)

调用方负责 engine、数据库 schema、Valkey / Temporal client 的生命周期。
所有执行使用 `start_runner()`，模型与工具资源由独立 Worker 管理。
创建、运行和关闭使用调用方提供的 Activity UUID，由 Temporal 在保留期内去重并保存执行记录。
数据库生命周期只含 creating/ready/closing/closed；Worker 执行时通过成对的
current_activity_id/current_activity_run_id 原子取得当前写入权，排队不占用 session。
所有进度保存校验完整身份；只有显式 close 查询 Temporal 并 CAS 接管终态或不存在的旧执行。
RPC 和 hooks 均不持有事务或显式锁。整轮最多一个 Temporal
尝试，不恢复 Agent 内部步骤。迁移将旧 creating/starting/running/closing 转为 closing，
保留 ready/closed、Config、State、快照和历史，清空旧占用并删除操作账本。

`live(session_id, after_seq=-1)` 返回历史回放并继续监听的异步 generator，
提前结束时使用 `aclosing`。消费者以最后完整应用消息的 `seq` 作为 `after_seq`，尚无该位置时使用 -1；
delta 不推进游标。完整消息写入后内容和位置固定，数据库中的数值缺口也保持封闭。
Pub/Sub 是尽力广播，断线后清空临时预览并从该位置续接。执行 Activity 仅在模型节点结束后记录消息，最终输出工具回执及 after-run
追加内容先保存在 runner_state，后续模型节点才记录。核心与 Telegram 的固定消息迁移
需离线执行，不兼容旧 Workflow 执行。
仅 model→tool、model→model 和 tool→model 转移最末原子保存消息与 layer State 快照，保留当前写入身份。
model→tool 在快照副本追加含 `RetryPromptPart` 的 request，内容为
`Unexpected interruption. Your messages may or may not be delivered. If your messages were intended to make changes, retry only if the operation is idempotent; otherwise, verify the outcome before taking further action.`；
model→model 和 tool→model 则追加尚未进入 history 的下一条 request，保留工具回执或重试提示。
所有中间快照的末条 request 标记为 SDK `interrupted`。恢复时总会请求模型；未获回执的调用
由 SDK 补齐 `outcome="interrupted"` 的回执并跳过。tool→end 不更新中间快照。
整轮成功后再保存最终快照并释放身份；失败保留最近保存的节点快照并进入 closing，不自动恢复执行。

业务 layers 在 [layer registry](src/kapy/layers/registry.py) 中统一组装，必须遵守以下约束：

1. 修改运行中的 history 时，只能修改已经带有 `metadata.seq` 的部分；最后一个已分配的
   `seq` 数值在修改前后必须相同（对应消息内容可以变化），其后尚未分配 `seq` 的消息
   必须保持原样。尚无已分配 `seq` 时，不得修改 history。
   保留的 `seq` 必须合法且严格递增，不得擅自为未编号消息分配 `seq`；修改后必须保持
   消息结构及工具调用/回执配对有效，尤其不能破坏跨越已编号/未编号边界的配对。
2. 所有业务 capability 定义的方法都可能被重试。hooks 不要求严格幂等，可酌情允许
   非 critical 的重复副作用，但重试不得造成 critical 的重复业务效果。
   工具只能响应模型的显式调用，并且执行前，该调用必须已记录在 model response
   末尾保存的 snapshot 中。实际执行必须与快照中的工具身份、调用标识和业务参数语义
   一致；不得从 hooks 或其他路径隐式调用工具，也不得静默改变已记录调用的语义。
3. 业务 layer 的 tool 或 hook 创建上游资源时，上游支持则应为资源或其 scope
   设置会话归属标识。标识根据当前会话的 `conversation_id`、`layer_id` 和固定
   Config 按需确定或派生，只使用定位本 conversation 资源所必需的信息，
   不得由模型指定或覆盖。
4. `exit_conversation` 应能重建归属标识，找全并回收所属资源，包括未写入 State
   的资源。上游不支持按归属查询或清理时，可依赖 State 尽力回收，但须在具体实现
   中说明可能遗漏资源并留下孤儿资源的局限。

Kapy 每次 Agent run 显式传入 `conversation_id=str(session_id)`；工具和 SDK hooks
从 `ctx.conversation_id` 读取身份，不从 State 或 Kapy deps 身份字段推断。
`enter_conversation(conversation_id, config)` 只在创建时初始化 State；
`exit_conversation(conversation_id, config, state)` 用于显式关闭和创建失败补偿。
这两个独立回调没有 RunContext，由宿主传入同一个字符串身份。State 只保存业务
进度及必要的回收记录。配置校验、默认 State 初始化与序列化无外部 I/O；enter
可执行业务 I/O。无 enter 时 State 必须能从 `{}` 初始化。

```sh
# 停止并保留 PostgreSQL 和 runtime 数据
docker compose down
# 清空本项目的容器和数据
docker compose down --volumes --remove-orphans
```
