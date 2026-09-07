# Gateway、CLI 与 Telegram 集成方案

本方案以 `kapy_v2.md` 和 `docs/architecture.md` 为边界，面向 Linux、单个控制进程、多 session、多 execution machine。实现范围为 `src/kapy/gateway/`、`src/kapy/cli/`、`src/kapy/settings.py` 及对应 tests。公共接口在总设计师批准后落地；本次提交只包含方案。

## 1. 依赖依据与模块边界

已执行 `uv sync --locked`，实际环境为 CPython 3.14.4、FastAPI 0.141.1、Starlette 1.6.0、httpx2 2.12.0、pydantic-settings 2.15.0、Typer 0.27.2、websockets 17.1、psycopg-pool 3.3.1、Valkey 6.1.1、Uvicorn 0.52.4。已通过本地 introspection 核对异步 HTTP、WebSocket、pool 和 settings alias API。现有依赖足够，不增加 Telegram SDK。

Gateway 只负责认证、路由、应用资源装配和前端适配。State 独占 session 串行化、输入缓冲、runner 调度、waiting、completion、output/history；Execution 独占 RPC 协议、daemon、进程、文件和本地代理；Intelligence 独占模型、runner、skill 内容及归档校验。

拟新增的职责文件：

| 路径 | 职责 |
| --- | --- |
| `kapy/settings.py` | 配置及显式环境变量别名 |
| `kapy/gateway/app.py` | FastAPI、lifespan、HTTP/机器 WS 入口 |
| `kapy/gateway/control.py` | 共用控制分发、参数校验、错误映射 |
| `kapy/gateway/machines.py` | 活跃 peer registry、双向调用、关联授权 |
| `kapy/gateway/auth.py` | 管理员、机器、session 调用者身份 |
| `kapy/gateway/telegram.py` | 前端插件、long polling、commands、输出发送 |
| `kapy/gateway/storage.py` | Telegram 表和参数化查询 |
| `kapy/cli/__init__.py`、`kapy/cli/commands.py` | `main`、Typer 命令、daemon/client 调用 |

## 2. Python 导出与应用生命周期

以下为拟议公共签名；签名中的 `...` 仅表示方案中的函数体省略。

```python
# kapy.gateway
from collections.abc import Callable, Sequence
from typing import Protocol
from fastapi import FastAPI
from pydantic import JsonValue
from psycopg_pool import AsyncConnectionPool
from kapy.settings import Settings

type JsonObject = dict[str, JsonValue]

class MachineCaller(Protocol):
    async def call(
        self, machine_id: str, method: str, params: JsonObject,
    ) -> JsonValue: ...

class ControlService:
    async def call(
        self, method: str, params: JsonObject, *, principal: Principal,
    ) -> JsonValue: ...

class Frontend(Protocol):
    async def run(self) -> None: ...

# frozen dataclass；pool 为借用资源，Frontend 不关闭它。
class FrontendContext:
    settings: Settings
    control: ControlService
    pool: AsyncConnectionPool

type FrontendFactory = Callable[[FrontendContext], Frontend]

def create_app(
    settings: Settings | None = None,
    *, frontends: Sequence[FrontendFactory] | None = None,
) -> FastAPI: ...

# kapy.cli
def main() -> None: ...

# kapy.settings
def load_settings(*, env_file: str | None = None) -> Settings: ...
```

`Principal` 是 Gateway 定义、不可由 RPC 参数反序列化的 frozen dataclass：`kind: Literal["operator", "session", "telegram"]`、`machine_id: str | None`、`session_id: str | None`、`telegram_route: tuple[int, int] | None`。HTTP/WS 入口根据认证结果构造，Telegram 根据允许的 chat 和 durable binding 构造。`ControlService` 不接受远端提交的 `Principal`；机器连接身份只提供来源证明，不能单独变成 operator。

`create_app` 无 import-time I/O。lifespan 依次创建并打开 PostgreSQL pool、Valkey client、HTTP client；运行集中迁移；创建 MachineRegistry、SkillService、runner 和 SessionService；完成 State 恢复后启动前端任务。registry 的授权回调通过 composition closure 引用已创建的 SessionService，所有对象装配后才接受业务请求。runner 只接收 machine caller 和 skill service 等依赖，State 接收 runner 回调，不形成互相导入。

一个 AnyIO task group 持有前端后台任务；机器连接任务由 ASGI 连接持有，registry 只保存活动 peer。关闭时停止前端输入、停止接受新业务请求，要求 State 停止调度并保存/取消运行中的本地 runner，再关闭机器 peer、HTTP client、Valkey 和 pool。控制端停止不能隐式杀死执行端仍运行的 PTY；执行端保留其自身进程生命周期。

构造中途失败由 `AsyncExitStack` 关闭已获得资源。State/Skills 若借用 pool 不自行关闭它。采用 FastAPI `lifespan`，其启动和退出资源边界已由[官方文档](https://fastapi.tiangolo.com/advanced/events/)确认。

`frontends=None` 时按配置装配 Telegram；显式空序列不启动插件。插件只调用 `ControlService`；只为 Telegram 的 durable mapping 使用借用 pool，不进入 session 内部状态机。CLI 通过本地代理访问同一 `ControlService`，不另建业务实现。

## 3. 配置与身份

`Settings` 使用 `env_prefix="KAPY_"`，以下五项通过 `Field(validation_alias=...)` 保留既有名字；alias 不被前缀覆盖，已做离线构造核对，规则见 [Pydantic settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)。默认不自动查找或读入任何 `.env`；仅 `load_settings(env_file=...)` 显式载入指定文件。

| 环境变量 | Python 字段与类型 | 默认/含义 |
| --- | --- | --- |
| `OPENAI_BASE_URL` | `openai_base_url: str` | `https://api.openai.com/v1` |
| `OPENAI_API_KEY` | `openai_api_key: SecretStr \| None` | 空；启动模型服务时要求可用 |
| `OPENAI_MODEL` | `openai_model: str` | `gpt-5.6-luna` |
| `TELEGRAM_BOT_TOKEN` | `telegram_bot_token: SecretStr \| None` | 空；未配置时不启用 Telegram |
| `TELEGRAM_CHAT_ID` | `telegram_chat_id: int \| None` | 空；启用 Telegram 必须提供允许的 chat |
| `KAPY_DATABASE_URL` | `database_url: SecretStr` | `postgresql://kapy:kapy-local@127.0.0.1:55432/kapy` |
| `KAPY_VALKEY_URL` | `valkey_url: SecretStr` | `redis://127.0.0.1:56379/0` |
| `KAPY_DATABASE_SCHEMA` | `database_schema: str` | `public`；只能为合法标识符 |
| `KAPY_VALKEY_NAMESPACE` | `valkey_namespace: str` | `kapy` |
| `KAPY_CONTROL_URL` | `control_url: str` | `http://127.0.0.1:8000` |
| `KAPY_CONTROL_TOKEN` | `control_token: SecretStr \| None` | 管理员 bearer；控制服务要求配置 |
| `KAPY_MACHINE_TOKENS` | `machine_tokens: dict[str, SecretStr]` | 控制端机器 id 到独立 bearer 的 JSON 映射 |
| `KAPY_MACHINE_ID` | `machine_id: str \| None` | daemon 身份 |
| `KAPY_MACHINE_TOKEN` | `machine_token: SecretStr \| None` | daemon 连接控制端的凭据 |
| `KAPY_SESSION_SIGNING_KEY` | `session_signing_key: SecretStr \| None` | 控制端稳定 HMAC 密钥 |
| `KAPY_SESSION_ID` | `session_id: str \| None` | daemon 为 session 子进程注入的来源上下文 |
| `KAPY_SESSION_TOKEN` | `session_token: SecretStr \| None` | session 子进程调用代理的 capability |
| `KAPY_DAEMON_SOCKET` | `daemon_socket: Path \| None` | 未设时取 Execution 的 XDG 默认 |
| `KAPY_TELEGRAM_API_BASE` | `telegram_api_base: str` | `https://api.telegram.org`；允许注入本地 fake |

空的可选 secret 和 chat id 规范化为 `None`；必需项按 `server`/`control-server` 启动路径校验，CLI `--help` 不要求数据库或模型凭据。新增配置由总设计师加入共享 `.env.example`，本分支不修改共享配置。模型 context window 等 Intelligence 配置沿用其最终导出，Gateway 不猜测模型能力。

机器凭据通过控制端配置注册，无额外 enrollment 服务或用户账号体系。HTTP `/rpc` 校验管理员 bearer；机器 `/rpc/machines/{machine_id}` 在升级前校验该机器独立 bearer。生产远程链路使用 TLS，loopback 开发地址保留 HTTP/WS。凭据不放 URL query、不回显 Settings、不记录 Telegram 带 token 的请求 URL。

session token 提议为 `v1.<session_id>.<machine_id>.<hmac>`，HMAC-SHA256 签名内容为固定用途前缀、canonical session id 和 machine id，签名校验使用 constant-time 比较。只有控制端持有 signing key；session 的存活与关联仍每次读 State。删除后 token 立即不能通过 session 存活检查；同 key 重启可重新导出同一 token，无明文 token 数据库和自动换 key。机器凭据本身不能伪造 session 身份。所有签名字段须使用无歧义编码，machine id 不允许包含 token 分隔符。

采用 Execution 的本地 NDJSON `proxy.call` → 机器 WS `control.proxy`。两层使用相同 params：`{method: string, params: object, session_id?: string, session_token?: string, operator_token?: string}`；外层 session_id/token 是来源，内层 params.session_id 是目标。必须恰好选择 session_id+session_token 或 operator_token 一种认证，Gateway 校验 token 的 machine_id 等于已认证连接的 machine id。operator_token 是显式提供的管理员 bearer，仅透传、不由 daemon 保存或自动补齐。省略 session token 不会升格为 operator。相同 OS 用户的进程不构成额外文件系统安全沙箱。

授权规则：operator 可管理该部署；session principal 必须关联承载连接的机器，只能操作自身和其直接创建的子 session，创建或更新可选机器都是来源 session 机器集的子集。递归由每个子 session 再创建下一层完成。State 保存不可由用户改写的 `created_by_session_id`。Telegram principal 只操作其 route 的已绑定 session；`/new` 只能替换该 route 的绑定。新建 session 所选机器均须存在于控制端配置，default_machine_id 必须在 machine_ids 中。

history、output、skill mutation 和 event 操作分别检查目标权限；知道 waiting id 不能获得任意 channel 的读写权。session token 可全局读取 skill 目录并创建新 skill，只能修改/删除自身创建的 skill；operator 可管理全部。该创建者元数据由 SkillService 持久化。

## 4. 机器 registry 与双向代理

机器连接使用 Execution 的 `RpcPeer`，Gateway 不实现第二套 JSON-RPC codec。Starlette WebSocket 的 send/receive/close 回调交给 peer，handler closure 固定连接的 machine id。机器反向请求只接受 `control.proxy`，剥离认证 envelope 后执行控制方法。控制端发往机器的方法只允许 `process.*`、`file.*`、`session.ensure`、`session.release`；每个请求都包含目标 `session_id`。

`MachineCaller.call` 先从 State 检查 session 未删除、机器已关联，再取活动 peer。首次使用该 session/machine 以及每次新连接时，调用幂等 `session.ensure`，将 session capability 和本地 CLI 所需上下文交给 daemon，由 daemon 建立 XDG cwd 并为之后的进程注入。并发 ensure 用单个 `(connection, session_id)` 锁合并；确保结果只在该连接内缓存。

拟议 `session.ensure` 参数为 `{session_id, session_token}`，结果为 `{session_id, cwd}`；session_token 绑定接收机器，Gateway 内部注入，不来自模型 tool 参数。daemon 为进程注入 `KAPY_MACHINE_ID`、`KAPY_SESSION_ID`、`KAPY_SESSION_TOKEN`、`KAPY_DAEMON_SOCKET`。同机不同 session 分属不同 XDG cwd，Gateway 不接收任意本地 cwd 替代此规则。

registry 保存 `machine_id -> connection instance`，同 id 新认证连接替换旧连接；旧连接的 finally 只有在当前值仍指向自身时才能删除 registry，旧连接也不能继续发起控制请求。关闭旧 peer 时让待决调用以明确断连错误结束。远程执行请求没有拿到 response 时属于结果未知，Gateway 不自动重放有副作用的执行请求。

离线机器最多等待 10 秒建立连接，再返回 `machine_offline`；await 不持有 registry 锁。Execution 可以保持常连，若启用 idle 断线须在 5 秒内定时重连，使控制端可以发现待处理请求；不新增唤醒服务。机器上线不自动重启进程，也不重新发送已经提交的命令。

建议单 frame 上限 1 MiB、单连接最多 32 个活跃 RPC、反向 control 并发上限 16。收包循环和 handler 分离，长 wait 不阻止响应、PTY 交互或递归调用；一个 await 不能持有串行发送锁。文件和大 stdio 仍走 Execution 的有界 chunk 接口，不把全量内容装入 frame。

## 5. 控制 JSON-RPC 合约

HTTP `POST /rpc`、机器反向请求、本地 CLI 和 Telegram 的进程内调用共用同一方法表。HTTP body 和 WS frame 使用同一 1 MiB 上限；业务仅接收 object params；envelope、id 回显、notification 无 response、batch 由 `kapy.rpc` 统一处理，遵循 [JSON-RPC 2.0](https://www.jsonrpc.org/specification)。HTTP 纯 notification 返回 204。CLI 发起需要确认的操作时总是携带 id。

下表 `?` 表示可省略；UUID 均为字符串；时间为 UTC ISO 8601；`JsonValue` 为 JSON 值。`request_key` 是调用方在重试前保存的幂等键，与 RPC id 分离；mutation 相同主体、方法、key、参数重放相同结果，key 冲突而参数不同返回冲突。Telegram 使用 `tg:<bot_id>:<update_id>:<action>`。外部读操作不经 ORM/Pydantic 二次验证数据库行。

公共结果形状：

| 名称 | JSON shape |
| --- | --- |
| `SessionConfig` | `{machine_ids: string[], default_machine_id: string, model: string, instructions: string}` |
| `SessionView` | `{session_id, config: SessionConfig, state: "idle"\|"running"\|"waiting"\|"deleting"\|"deleted"\|"failed", created_by_session_id: string\|null, created_at, updated_at}` |
| `Submission` | `{session_id, input_id: string\|null, waiting_id, request_key}` |
| `OutputItem` | `{cursor: string, kind: "text_delta"\|"message"\|"waiting"\|"error"\|"deleted", payload: JsonValue, created_at}`；text_delta payload 为 `{message_id: string, text: string}`，message 为 `{message_id, role: string, content: JsonValue, streamed: bool}`，waiting 为 `{waiting_id: string}`，error 为 `{code: string, message: string}`，deleted 为 `{session_id: string}` |
| `OutputPage` | `{items: OutputItem[], next_cursor: string, has_more: bool}` |
| `CompletionPage` | `{events: [{cursor, session_id, waiting_id, state, output_cursor}], next_cursor: string, timed_out: bool}` |
| `HistoryPage` | `{items: [{history_id, role, content: JsonValue, created_at}], next_cursor: string\|null}` |
| `SkillInfo` | `{skill_id, name, description, sha256, archive_bytes, created_by_session_id: string\|null}` |

cursor 为 State 返回的 opaque string，只能原样带回；没有 cursor 表示从头。`limit` 默认 100、范围 1–500，同时以 512 KiB 页面字节数封顶。单条大 payload 的 chunk/引用由 State 输出模型提供，不能让一条历史或输出撑爆 frame。

### Session、输入与输出

| 方法 | params | result / 语义 |
| --- | --- | --- |
| `session.create` | `{config: SessionConfig, input?: {text, mode?: "steer"\|"queue"}, waiting_id?: string, request_key: string}` | `Submission`；默认 mode=queue；立刻返回，State 异步调度 |
| `session.get` | `{session_id}` | `SessionView` |
| `session.list` | `{cursor?: string, limit?: int}` | `{items: SessionView[], next_cursor: string\|null}`；服务端在分页前筛权限 |
| `session.update` | `{session_id, config: SessionConfig, request_key}` | `SessionView`；完整替换配置，在 runner 安全边界生效 |
| `session.delete` | `{session_id, request_key}` | `{session_id, state: "deleting"\|"deleted"}`；State 停止调度并保留可恢复清理任务 |
| `session.input` | `{session_id, text: string, mode?: "steer"\|"queue", waiting_id?: string, request_key}` | `Submission`；前端默认 queue，显式 steer 插入运行间隙 |
| `session.output` | `{session_id, after?: string, limit?: int, timeout?: float}` | `OutputPage`；timeout 默认 0、范围 0–30 秒 |
| `session.wait` | `{session_id, waiting_id, after?: string, timeout?: float}` | `CompletionPage`；timeout 默认 30、范围 0–30 秒 |

`session.create` 未携带 input 时创建一个 waiting session，仍返回可观察的完成标识。携带 input 或 `session.input` 返回的 waiting id 指向该次提交的完成；请求方提供 waiting id 时，Gateway 校验来源对该 completion channel 的投递授权，State 在相应输入被消费并进入 waiting 后向该 channel 投递一次完成。不能因目标此前已经 waiting 而提前判完成。多个提交被同一轮消费时由 State 记录和完成对应 receipts；复用 waiting id 时按 completion cursor 区分多次完成。

`session.wait` 是 durable completion 的非消费读取，HTTP/CLI 超时不取消 session 或 input、不消费 event、不占用 agent subscriber。`session.output` 先读 durable cursor 后有限等待，再重查数据库，Valkey 只是 wakeup hint；CLI 和 Telegram 均通过这个接口重放再接实时输出，不维护另一套内存历史。

配置更新不改变当前模型调用参数；下一运行边界由 State/Intelligence 接管。移除仍有活跃执行资源的机器关联时返回冲突，避免使清理资源失去授权。Execution 提供幂等 `session.release({session_id}) -> {session_id, released: bool}`，终止该 session 的进程树并释放句柄。State 持久化删除清理目标；Gateway 的内部清理调用只允许以该 tombstone 中的机器关联发送 release，不走拒绝 deleted session 的普通 tool caller。离线机器恢复后继续清理，不能以 WS 断线代替删除。

### 事件与历史

| 方法 | params | result / 语义 |
| --- | --- | --- |
| `event.publish` | `{session_id, waiting_id, payload: JsonValue, mode?: "steer"\|"queue", request_key}` | `{event_id, waiting_id}`；非 UI 默认 steer；来源由 principal 注入 |
| `history.list` | `{session_id, after?: string, limit?: int}` | `HistoryPage` |
| `history.search` | `{session_id, query: string, mode: "substring"\|"fulltext", after?: string, limit?: int}` | `HistoryPage` |
| `history.query` | `{session_id, sql: string, parameters?: JsonValue[]}` | `{columns: string[], rows: JsonValue[][], truncated: bool}` |

用户界面不公开 subscriber 创建/ack；agent wait 工具走 State 的 runner context。`event.publish` 仅允许调用者有权生产的目标 session/channel；State 保存 channel 归属并处理广播、滞留、steer/queue、排除自身，不由 Gateway 仿制。history SQL 原文传给 State 的受限查询实现，Gateway 不拼接 WHERE、不开放 PostgreSQL 通用连接；substring、全文检索的实现与多语言策略归 State。

### Skills

| 方法 | params | result |
| --- | --- | --- |
| `skill.list` | `{query?: string, cursor?: string, limit?: int}` | `{items: [{skill_id, description}], next_cursor: string\|null}`；query 子串过滤 |
| `skill.get` | `{skill_id}` | `SkillInfo` |
| `skill.read` | `{skill_id}` | `{skill_id, markdown: string}`；完整 SKILL.md |
| `skill.create` | `{session_id, machine_id?: string, archive_path: string, request_key}` | `SkillInfo` |
| `skill.update` | `{skill_id, session_id, machine_id?: string, archive_path: string, request_key}` | `SkillInfo` |
| `skill.download` | `{skill_id, session_id, machine_id?: string, archive_path: string, request_key}` | `{skill_id, archive_path: string, sha256: string, archive_bytes: int}` |
| `skill.delete` | `{skill_id, request_key}` | `{skill_id, deleted: bool}` |

skill 归档在获授权 execution session 的工作目录中交换，未指定 machine 时使用该 session 默认机器。Gateway 通过 Execution 已有 file.pull/chunk/finish 从 archive_path 获取有界 zip，再调用 Skills 原子 create/update；download 取同一版本完整 archive 后通过 file.push/chunk/finish 写回 archive_path。路径、覆盖规则与文件传输 hash 校验由 Execution 执行；不新增 skill upload 会话协议。

archive 上限拟 16 MiB、文件 chunk 上限 64 KiB，完整归档只在最终调用 SkillService 的有界 bytes 参数中出现。每次 Gateway 进程最多并行两次归档交换，临时文件在成功、取消或失败后删除；中断重新传输，不要求重启续传。Skills 保存 metadata 与 bounded archive bytea 的同一版本，负责 extraction 限额、归档验证和最终发布幂等。完整 SKILL.md 上限拟 64 KiB，确保全文可由单个 RPC 返回。CLI 的安全 pack/extract 复用 Intelligence 导出。

错误使用标准 JSON-RPC 错误加 application code：`-32001 unauthorized`、`-32003 forbidden`、`-32004 not_found`、`-32009 conflict`、`-32010 machine_offline`、`-32011 disconnected`、`-32012 limit_exceeded`。`error.data` 只含稳定 `kind`、安全上下文和可选 `retry_after`，不带凭据、内部 DSN 或原始堆栈。业务重试只用于带 request_key 的 mutation 和幂等读写。

## 6. 对 State、Execution、Intelligence 的 Python 需求

State 需提供下列具名业务入口；DTO 的精确字段对应上一节。类型由 State 所有，Gateway 只在外部边界做参数 validation，避免 State 依赖 Gateway：

```python
# kapy.state 的拟议接口；AuthorizationScope 只供可信进程内调用。
class SessionService:
    def __init__(
        self, pool: AsyncConnectionPool, valkey: Valkey, *,
        runner: Runner, namespace: str,
    ) -> None: ...
    async def start(self) -> None: ...
    async def aclose(self) -> None: ...
    async def create_session(
        self, config: SessionConfig, *, text: str | None = None,
        mode: InputMode = "queue", waiting_id: str | None = None,
        created_by_session_id: str | None = None, request_key: str,
    ) -> Submission: ...
    async def get_session(self, session_id: str) -> SessionView: ...
    async def list_sessions(
        self, *, scope: AuthorizationScope, after: str | None = None,
        limit: int = 100,
    ) -> SessionPage: ...
    async def update_session(
        self, session_id: str, config: SessionConfig, *, request_key: str,
    ) -> SessionView: ...
    async def delete_session(
        self, session_id: str, *, request_key: str,
    ) -> SessionView: ...
    async def submit_input(
        self, session_id: str, text: str, *, mode: InputMode = "queue",
        waiting_id: str | None = None, request_key: str,
    ) -> Submission: ...
    async def read_output(
        self, session_id: str, *, after: str | None = None,
        limit: int = 100, timeout: float = 0,
    ) -> OutputPage: ...
    async def wait_completion(
        self, session_id: str, waiting_id: str, *, after: str | None = None,
        timeout: float = 30,
    ) -> CompletionPage: ...
    async def publish_event(
        self, session_id: str, waiting_id: str, payload: JsonValue, *,
        producer_session_id: str | None, mode: InputMode = "steer",
        request_key: str,
    ) -> EventReceipt: ...
    async def list_history(
        self, session_id: str, *, after: str | None = None, limit: int = 100,
    ) -> HistoryPage: ...
    async def search_history(
        self, session_id: str, query: str, *, mode: SearchMode,
        after: str | None = None, limit: int = 100,
    ) -> HistoryPage: ...
    async def query_history(
        self, session_id: str, sql: str, *, parameters: Sequence[JsonValue] = (),
    ) -> QueryResult: ...
```

`AuthorizationScope` 提议为 frozen dataclass：`kind: Literal["all", "session", "ids"]`、`session_id: str | None`、`session_ids: tuple[str, ...]`。State 仅将可信 Gateway 给出的 scope 转成参数化过滤；session scope 包含自身和直接子 session。所有幂等键由 Gateway 增加认证主体命名空间，State 持久化参数摘要和原始结果。session 关联、创建者、配置属于 State authoritative schema。

Execution 提供以下导出建议，最终以双方统一签名为准：

```python
# kapy.rpc
from collections.abc import Awaitable
type RequestHandler = Callable[[str, JsonObject], Awaitable[JsonValue]]

class RpcPeer:
    def __init__(
        self, send_text: Callable[[str], Awaitable[None]],
        receive_text: Callable[[], Awaitable[str | None]],
        close_transport: Callable[[], Awaitable[None]],
        handler: RequestHandler,
    ) -> None: ...
    async def __aenter__(self) -> RpcPeer: ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    async def call(
        self, method: str, params: JsonObject, *, timeout: float = 60,
    ) -> JsonValue: ...
    async def wait_closed(self) -> None: ...
    async def aclose(self) -> None: ...

async def dispatch_json(payload: str, handler: RequestHandler) -> str | None: ...

# kapy.execution
async def run_daemon(config: DaemonConfig) -> None: ...
async def local_control_call(
    method: str, params: JsonObject, *, socket_path: Path | None = None,
    session_id: str | None = None, session_token: str | None = None,
    operator_token: str | None = None, timeout: float = 35,
) -> JsonValue: ...
```

Gateway 将 `Settings` 显式转换为 Execution 自有 `DaemonConfig`，不要求 Execution 导入 Settings。Starlette 的断连异常转换为 receive_text 返回 None。`local_control_call` 负责 NDJSON、本地连接和 proxy envelope，CLI 负责保存 request_key 和展示结果，不能每次调用产生不同幂等键后自动重试。`dispatch_json` 为 HTTP 路由所需的共享 envelope/error/batch helper。

Intelligence 提供 `Runner(config: RunnerConfig, machine_caller: MachineCaller, skills: SkillService)`，其 `async __call__(context: RunContext, inputs: Sequence[InputItem], history: Sequence[HistoryItem]) -> RunResult` 可直接注入 State。Intelligence/State 共同确定这些运行 DTO；Gateway 只装配。Skills 的拟议公开入口使用 bounded bytes，文件 chunk 和 Gateway 临时文件不进入 Skills 持久化协议：

```python
class SkillService:
    def __init__(self, pool: AsyncConnectionPool) -> None: ...
    async def list(
        self, *, query: str | None = None, after: str | None = None,
        limit: int = 100,
    ) -> SkillPage: ...
    async def get(self, skill_id: str) -> SkillInfo: ...
    async def read(self, skill_id: str) -> str: ...
    async def create(
        self, archive: bytes, *, created_by_session_id: str | None,
        request_key: str,
    ) -> SkillInfo: ...
    async def update(
        self, skill_id: str, archive: bytes, *, request_key: str,
    ) -> SkillInfo: ...
    async def get_archive(self, skill_id: str) -> bytes: ...
    async def delete(self, skill_id: str, *, request_key: str) -> None: ...

def pack_skill(source_dir: Path, archive_path: Path) -> None: ...
def extract_skill(archive_path: Path, destination: Path) -> None: ...
```

## 7. CLI 命令形状与上下文

总设计师在 `pyproject.toml` 添加 `[project.scripts] kapy = "kapy.cli:main"`，使安装包可由 uvx 调用。Typer 只解析命令，异步工作在一个 asyncio/AnyIO 运行入口执行；`control-server` 使用 Uvicorn 的 uvloop 配置，避免嵌套 event loop。

| 命令 | 动作 |
| --- | --- |
| `kapy server` | 调用 Execution `run_daemon`，管理本机和 outbound WS |
| `kapy control-server` | 运行 Gateway app，固定单 worker |
| `kapy control session create/get/list/update/delete` | 对应 session CRUD |
| `kapy control session input` | 提交文本，`--mode steer\|queue`、可选 `--waiting-id` |
| `kapy control session output --follow` | 带 cursor 重放后 long poll，逐条 JSON line |
| `kapy control session wait` | 有限等待 receipt，可重复读取 |
| `kapy control event publish` | 向获授权的 waiting channel 投递 |
| `kapy control history list/search/query` | session 范围历史；SQL 支持 stdin/文件 |
| `kapy control skill list/get/read/upload/download/delete` | 全量/筛选描述、全文、完整 archive/folder |

`--session` 指定目标，未指定时从 `KAPY_SESSION_ID` 获取；来源 session id/token 始终独立取环境上下文。本地 socket 由参数、`KAPY_DAEMON_SOCKET`、XDG 默认依次决定。无 session 上下文时要求显式 `KAPY_CONTROL_TOKEN` 作为 operator_token 透传；有 session token 时始终按 session 身份代理，即使环境中还存在 control token。CLI 默认经本地 daemon，不另造直连控制端的旁路。

mutation 默认生成 request_key，并输出含 request_key/session_id/waiting_id 的 JSON；`--request-key` 允许调用者复用。长 prompt 和 SQL 支持 stdin/文件，token 仅用环境/受保护配置，不设计 argv token 选项。stdout 只输出结果/JSON lines，stderr 输出错误；Ctrl-C 仅停止本次 follow/wait，不删除 session、不打断远程进程。错误返回非零退出码；无事件的有限等待属于正常超时结果。

upload 在目标 session 的本机 XDG cwd 内暂存 zip，调用 `skill.create/update`，结束后删除自己创建的暂存文件。download 指定同一 session 内未存在的暂存 archive_path，待 file.push 完成后在本地校验 hash、安全解包并 rename 到目标目录；拒绝覆盖非空目标。CLI 通过 Execution 的 XDG 路径导出定位该 cwd，不复制目录算法；若使用远程 machine，RPC 返回文件在该机器的路径，CLI 不假定它是本机路径。pack/extract 的安全规则重用 Skills 导出。

## 8. Telegram routing、设置与持久化

Telegram 作为 `Frontend` 使用 httpx2 异步 Bot API client；只启用一个 long poller，`getUpdates(timeout=25, limit=100, allowed_updates=["message"])`，HTTP read timeout 大于 long poll timeout。`TELEGRAM_CHAT_ID` 是唯一允许的 chat，未配置不能进入开放接收模式；忽略其他 chat、机器人消息，非文本输入回复明确的文本输入提示并消费 update。普通消息/commands 都带相同 chat/topic 授权。群迁移时记录明确错误并保留状态，由配置更新允许的 chat；不自动改变 allowlist。

route key 为 `(bot_id, chat_id, thread_id)`，没有 `message_thread_id` 时将 thread_id 规范化为 0；发送时 0 省略，非零原样传回。不同 topic 的 config、active session 和 cursor 独立。`bot_id` 使用配置 token 的公开数字前缀，不保存 token。

Gateway 拥有以下 PostgreSQL 表定义和参数化查询；由 State/architect 的集中迁移入口装配。schema 参数由连接 search_path 或受控 SQL Identifier 设置，不能插入未校验文本。

| 表 | 核心字段与约束 |
| --- | --- |
| `gateway_telegram_poll` | `bot_id PK, next_update_id bigint` |
| `gateway_telegram_inbox` | `(bot_id, update_id) PK, chat_id bigint, thread_id bigint, payload jsonb, resolved_action jsonb nullable, handled bool` |
| `gateway_telegram_routes` | `(bot_id, chat_id, thread_id) PK, session_id UUID nullable, config jsonb` |
| `gateway_telegram_delivery` | `(bot_id, chat_id, thread_id, session_id) PK, cursor text nullable, item_offset int, next_attempt_at timestamptz nullable, blocked_error text nullable` |

poller 先用一笔 PostgreSQL 事务插入整批 inbox（主键去重）并持久化下次 offset，再向 Telegram 请求更高 offset；因此提前确认的 update 已在本地耐久保存。另一个处理循环按 route 顺序处理 inbox，先保存 resolved_action（目标 session、完整配置、text 和 request_key），再调用 ControlService；回复成功或确定为永久回复错误后标记 handled。崩溃发生在 State 提交之后、inbox 标记之前时，重放获得原结果，不重复创建 session 或输入。无关/不支持的 update 也明确完成，避免反复卡住 offset；临时错误保持待处理、定时退避，不阻塞其他 route。

route 每次只串行处理一个命令；`/new` 先以幂等 key 创建 session，再提交 route 指针，恢复时仍得到同一 session。无 active session 的第一条文本按该 route 保存配置创建并提交 input。没有保存 machine 时：恰好一台配置机器则取它，否则发送设置提示并保持无 session；不猜测执行机器。

| command | 行为 |
| --- | --- |
| `/new` | 从该 route 已保存 config 创建新 session，旧 session 仍可查询 |
| `/settings` | 展示该 route 的非秘密配置 |
| `/model <name>` | 保存模型配置并更新 active session 下轮配置 |
| `/machine <id>` | 保存默认机器；必须来自已配置机器，更新 route 的 machine 集和 active session |
| `/instructions <text>` | 保存额外指令并更新 active session 下一运行边界 |
| `/steer <text>`、`/queue <text>` | 明确输入模式；普通文本默认 queue |
| `/status` | session 当前状态 |
| `/help` | command 列表和简短参数说明 |

配置命令在 inbox 中保存解析后的完整目标配置；先完成幂等 session.update，再提交 route.config，重放不会遗漏任一端。新 session 不复制旧 history，只继承保存配置。`setMyCommands` 注册这些 commands；不存在 active session 时也可先保存设置。

### 输出发送和恢复

每个有待输出的 delivery 从已确认 cursor 读取 `session.output`，以约一秒或达到长度上限为批次合并 text delta，再追加消息；waiting/final/error 到来时刷新剩余文本。State 的 message_id 关联 delta 与最终 message，`streamed=true` 明确表示完整可见正文已按此前 delta 输出，Telegram 跳过最终 message 正文；false 才发送正文。该标记须由 State 持久化，重启也无需前端记忆哪些消息已流式输出。不同 session 的 delivery 独立，`/new` 后旧 session 的未完成输出仍按原 route 发送，并标注简短 session id。

消息使用纯文本、不设置 parse_mode；以最多 4000 UTF-16 code units 分段并保持 Unicode 字符完整，留出 session 标注空间。每个 chat 的所有 topic 共用发送限流，429 后按服务端延迟继续。Bot API 支持 topic 路由，文本上限为 4096 字符，429 提供 `retry_after`；poll offset 的确认语义和这些参数以[官方 Bot API](https://core.telegram.org/bots/api)为依据。

发送成功才推进 cursor。cursor 表示最后完整发送/跳过的 item，item_offset 表示下一个 item 已发送的 Unicode 字符数；一个 item 拆成多段时逐段保存 offset，全部完成才移动 cursor。后续正文从 durable output 重建，无需另一份持久化文本或 Telegram message_id。合并多 item 的一条消息成功后一次提交末端位置；失败重试同一范围。删除 session 后 State 仍须保留待消费输出和 message/delta 关联，直至 delivery 完成。

网络错误/5xx 指数退避（1–30 秒加 jitter），429 尊重 retry_after；发送位置和 next_attempt_at 持久化。401 停用该 bot 任务并暴露错误，403 或 topic 不可发送时只阻塞该 route，保留 cursor，不无限热循环；后续该 route 的有效输入可触发一次恢复。其他 route 继续工作。

Telegram 已接受 send 但响应丢失，或在本地发送 cursor 提交前崩溃时，重试可能重复，不能宣称 exactly-once。分段追加会产生多条聊天消息。重启从 durable inbox 和各 delivery cursor 继续，不依赖内存订阅仍在。不会主动丢弃已有 webhook 的更新；若 Bot API 报 webhook/poller 冲突则明确报告并停止 poller。

## 9. 持久化边界与集成约束

PostgreSQL 是 session、输入、history/output、event、completion 和 Telegram ingress/delivery 的权威存储；Valkey 只提供唤醒提示，Gateway registry 是连接事实的内存映射。没有将控制状态改用 SQLite/内存的路径。Execution 的 XDG SQLite/进程资源归 Execution。

每次集成调用使用独立 `KAPY_DATABASE_SCHEMA=gw_<uuid>`、`KAPY_VALKEY_NAMESPACE=gw:<uuid>`，使用总设计师提供的 PostgreSQL/Valkey 地址；清理只作用于自己的 schema/namespace。Gateway 与其他 senior 不共享测试路由、session ids、上传临时目录或 XDG root。Telegram send/getUpdates/setMyCommands 全部对 fake Bot API 或 MockTransport，不发送真实消息；本轮没有读取主目录 `.env`。已只读查看 main 的 `docs/acceptance.md`（61af09e），按其 Gateway 并发 polling、机器中断、Telegram 恢复场景提供对应 module tests；不重启或 flush 共用开发服务。

跨模块交付要求：State 提供可恢复 receipts、输入幂等、scope 分页、output cursor、session 关联/创建者以及集中迁移接入；Execution 提供共享 RPC codec、daemon/local client、session context 注入和删除清理；Intelligence 提供 runner factory、skill 数据与安全传输/归档能力。总设计师统一导出和 DTO 名称、加入 CLI script 和新增 settings 示例，并协调上述共享 schema。Gateway 不并行修改其他 senior 包或共享 `pyproject.toml`、`uv.lock`、`compose.yaml`、README。
