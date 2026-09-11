# Kapy v2

当前开发栈围绕 `src/kapy/tmpv2` 的 Python 模块：本地进程与 PTY、文件传输、
Agent Runner、上下文压缩，以及基于 Valkey Pub/Sub 的实时输出和历史回放。
HTTP 和 Telegram 作为独立进程插件，直接调用这些服务。HTTP 同时挂载配置前端。

## 本地启动

需要 Docker Compose。镜像提供 Python 3.14 和锁定版本的开发依赖。
首次配置可以复制 `.env.example` 为 `.env`；已有 `.env` 直接复用。
填写 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。
镜像构建时使用 npm 生成前端产物，无需手动维护 `dist`。

```sh
docker compose build runtime
docker compose up -d --wait postgres valkey
# 首次启动：核心和 Telegram 数据库分别升级，serve 不自动迁移。
docker compose run --rm runtime kapy db upgrade
docker compose run --rm telegram kapy plugin telegram db upgrade
docker compose up -d runtime telegram
docker compose ps
```

| 服务 | 用途 | 宿主机入口 |
| --- | --- | --- |
| `postgres` | Runner checkpoint、原始 history、compaction、输入队列及取消状态 | `127.0.0.1:55432` |
| `valkey` | 按 session 广播临时输出；无离线消息、TTL 或持久化 | `127.0.0.1:56379` |
| `runtime` | 非 root 的 HTTP 插件和配置前端 | `0.0.0.0:8000`，前端 `/app/` |
| `telegram` | 独立 Telegram session 插件，通过 Bot API 长轮询 | 无入站端口 |

源码、测试和脚本挂载为只读；更新依赖或前端后需要重新构建。
PostgreSQL、runtime 和 Telegram 状态目录分别保存在 `postgres-data`、
`runtime-data`、`telegram-data` 命名卷。ProcessManager 默认使用
`/var/lib/kapy/state/kapy` 保存 SQLite 和进程输出；Telegram 使用独立卷中的
`/var/lib/kapy/state/kapy/plugins/telegram/telegram.sqlite3`。

Compose 从 `.env` 读取 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL`、
`KAPY_DATABASE_SCHEMA` 和 `KAPY_VALKEY_NAMESPACE`。容器内数据库和 Valkey URL
使用 Compose 服务名；`.env.example` 的 URL 用于宿主机直接调用模块。
Telegram 复用 `.env` 中的 bot token 和 chat ID，
也可用 `KAPY_TELEGRAM_ALLOWED_CHAT_IDS` JSON 列表配置多个 chat。
启动不创建 provider 或 model。配置前端在 `http://localhost:8000/app/`。
HTTP API、WebSocket 和前端直接访问，无需登录或访问 token。
创建模型后在 Telegram 发送 `/model <provider UUID> <model name>`，同时更新当前
聊天/话题的 session 模型并保存 bot 默认选择，后续新会话也使用它；选择重启后保留。
`/model` 不带参数显示当前默认值。尚未选择模型时 bot 仍可启动，并提示配置命令。
`KAPY_TELEGRAM_SESSION_TEMPLATE` 可作为没有已保存选择时的初始默认模板。
可用 `KAPY_HTTP_PORT` / `KAPY_POSTGRES_PORT` / `KAPY_VALKEY_PORT` 更改映射端口。

## 验证

全部 tmpv2 测试使用真实 PostgreSQL、Valkey、子进程、PTY 和本地 HTTP 服务；
模型行为使用 SDK 的确定性测试模型，不调用外部模型 API。
进程和文件测试要求在容器中运行，避免在宿主机跳过。

```sh
docker compose exec -T runtime python -m pytest -q -p no:cacheprovider tests/tmpv2
```

使用 `.env` 的模型端点和凭据运行真实模型验收：

```sh
docker compose exec -T runtime python scripts/check_tmpv2.py
```

该脚本使用 OpenAI Responses 协议，会产生数次模型请求。运行前需按上方步骤
执行 `kapy db upgrade` 初始化配置的 PostgreSQL schema；脚本保留一个独立
session 供检查，每次运行使用新的 session ID。

验收内容：

- 模型调用工具，通过 ProcessManager 启动进程生成文件；检查输出、文件原子写入，
  并重新打开 ProcessManager 读取保留的进程结果。
- SessionService 开启实时输出（默认每 0.5 秒批量发送 delta）；消费端先订阅，
  再读历史。按顺序消费事件批次，检查完整消息 DTO 与数据库一致；临时 delta
  按 part 还原预览，完整消息替换预览。
- 从指定 history seq 回放；手动压缩保持原始 history 不变；重启后仅用摘要恢复，
  验证模型仍记得先前工具结果，并触发基于 usage 的自动压缩。
- 关闭实时输出仍正常落库；重新连接补齐未广播的历史，退出后释放订阅。

真实模型是否返回可见的 thinking delta 取决于提供方；脚本单独输出其数量。
工具调用及结果从完整消息读取，不使用文本 delta 表达工具参数。
测试套件另外覆盖竞争租约、心跳丢失、checkpoint 恢复、取消、失败清理等边界。

## 模块入口

- [进程管理 API](src/kapy/tmpv2/processes/README.md)
- [文件传输](src/kapy/tmpv2/file_transfer.py)
- [Runner、压缩和实时输出契约](src/kapy/tmpv2/agent_runner/README.md)
- [Valkey 输出服务](src/kapy/tmpv2/agent_output/service.py)
- [SessionService：生产侧与历史回放](src/kapy/tmpv2/control/sessions/service.py)
- [独立进程接口插件](src/kapy/tmpv2/plugins/README.md)
- [核心与插件数据库迁移](src/kapy/tmpv2/database/README.md)
- [控制面 FastAPI HTTP / WebSocket API](src/kapy/tmpv2/plugins/http/README.md)

调用方负责 engine、数据库 schema、模型 Agent 和 Valkey client 的生命周期。
`SessionService.start_runner(..., realtime_output=True)` 需要注入
`AgentOutputService`；默认关闭实时输出。`live(session_id, after_seq=-1)`
返回从最后已应用完整消息序号之后回放、再继续监听的异步 generator，提前结束时使用 `aclosing`。
Pub/Sub 是尽力广播，断线后用最后一条完整消息的 `seq` 作为 `after_seq` 续接历史。

```sh
# 停止并保留 PostgreSQL 和 runtime 数据
docker compose down
# 清空本项目的容器和数据
docker compose down --volumes --remove-orphans
```
