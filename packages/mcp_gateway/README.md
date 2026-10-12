# MCP Gateway

独立 Python 包 `kapy-mcp-gateway`，通过 `import mcp_gateway` 使用，入口为
`mcp-gateway` 或 `python -m mcp_gateway`。依赖 FastMCP 4.1，不依赖 `kapy.*`、
PostgreSQL、Valkey 或 Temporal。

它把 `{name: MCP server}` 聚合为一个 MCP server，工具和 prompt 名称使用 `.` 分隔。
inline 模式通过私有 stdio 管道连接，在创建子进程时传入上游位置与凭据，不使用 JWE。
HTTP 模式通过 Streamable HTTP 连接，每个客户端使用
`Authorization: Bearer <compact JWE>`，Gateway 解密后注入对应上游凭据。
代理能力使用 [FastMCP ProxyProvider](https://gofastmcp.com/servers/providers/proxy)，
鉴权接入 [TokenVerifier](https://gofastmcp.com/servers/auth/token-verification)。

## 安装与启动

从仓库根目录安装、运行这个 workspace 包：

```sh
uv sync --locked --package kapy-mcp-gateway
uv run --locked --package kapy-mcp-gateway mcp-gateway --help
```

也可以单独安装目录或构建 wheel：

```sh
pip install ./packages/mcp_gateway
uv build --package kapy-mcp-gateway
```

## Inline stdio

`mcp-gateway stdio` 从环境变量 `MCP_GATEWAY_INLINE_CONFIG` 读取完整 JSON 配置，
也可使用 `--config-json '<JSON>'`。配置在每个 Gateway 创建时提供，不需要配置文件、
JWE 或共享密钥；stdout 专供 MCP 协议，启动错误和日志写 stderr。
每个 Gateway 暴露其 `servers` 中的全部上游，省略凭据表示使用固定 header/env
或匿名访问。空 `servers` 合法。

```json
{
  "servers": {
    "github": {"transport": "http", "url": "https://github-mcp.example.com/mcp"},
    "local": {
      "transport": "stdio",
      "command": "uvx",
      "args": ["example-mcp-server"],
      "credential_env": ["API_KEY"]
    }
  },
  "credentials": {
    "github": {"bearer": "upstream-access-token"},
    "local": {"env": {"API_KEY": "local-api-key"}}
  }
}
```

从 Python 客户端启动私有 Gateway：

```python
import sys
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp_gateway import INLINE_CONFIG_ENV, InlineGatewayConfig


async def use_inline(config: InlineGatewayConfig) -> None:
    transport = StdioTransport(
        sys.executable,
        ["-m", "mcp_gateway", "stdio"],
        env={INLINE_CONFIG_ENV: config.startup_json()},
        keep_alive=False,
    )
    async with Client(transport) as client:
        print(await client.list_tools())
        print(await client.call_tool("github.search", {"query": "FastMCP"}))
```

`InlineGatewayConfig.model_validate(...)` 验证配置但不连接上游。
普通 repr / JSON 导出遮盖凭据；`startup_payload()` / `startup_json()` 显式导出明文，
用于传给子进程。构造 `StdioTransport` 不启动进程，进入客户端上下文才启动，退出时关闭。
启动配置不会自动传给 stdio 上游；SDK 的基础环境之外，只有其固定 `env` 和允许注入的
`credential_env` 会传入。
配置按实例隔离，创建后固定；更新上游集合需创建新的 Gateway。

也可调用 `create_stdio_gateway(config, auth_factories=...)` 获得无鉴权的 FastMCP 实例，
然后运行 `gateway.run(transport="stdio", show_banner=False)`。Python 入口支持可信鉴权
适配器；通用 CLI 接受 Bearer、Basic、headers 和 stdio env，不加载自定义 Python 代码。
Kapy 使用这一 stdio 路径，见 [MCPs layer](../../src/kapy/layers/README.md#mcps-layer)。

## HTTP / JWE

写入 `gateway.json`。URL、命令、工作目录和允许注入的环境变量都由服务端确定，
JWE 无法修改这些位置。namespace 区分大小写，限小写字母开头的
`[a-z][a-z0-9_-]{0,31}`，不包含分隔符 `.`。

```json
{
  "name": "Team MCP Gateway",
  "issuer": "team-token-issuer",
  "audience": "https://gateway.example.com/mcp",
  "servers": {
    "github": {"transport": "http", "url": "https://github-mcp.example.com/mcp"},
    "legacy": {"transport": "sse", "url": "https://legacy.example.com/sse"},
    "local": {
      "transport": "stdio",
      "command": "uvx",
      "args": ["example-mcp-server"],
      "credential_env": ["API_KEY"]
    }
  }
}
```

HTTP/SSE 可配置固定 `headers`；stdio 可配置固定 `env` 和 `cwd`。
grant 的同名 header/env 优先于固定值，Bearer/Basic/鉴权适配器优先于固定
Authorization。HTTP header 名称统一为小写，拒绝重复名称、换行及影响传输的
Host、MCP session ID 等 header。每个上游的 `timeout` 默认 30 秒。
默认与下游保持同一协议模式；只支持旧握手协议的上游可配置 `"mode": "legacy"`。

离线签发方和 Gateway 共用一个随机 256-bit 密钥。启动时必须显式提供密钥，
不生成临时默认密钥。保存并在重启时复用它；只有可信签发方和 Gateway 持有它。

```sh
export MCP_GATEWAY_JWE_KEY="$(uv run --locked --package kapy-mcp-gateway mcp-gateway keygen)"
uv run --locked --package kapy-mcp-gateway mcp-gateway serve --config gateway.json
# 默认监听 http://127.0.0.1:8001/mcp
# 可配置 --host、--port、--path；ASGI 嵌入见下文。
```

创建只对签发方可读的 `credentials.json`，顶层为 namespace → grant。
省略的上游既不出现在列表里，也不能被直接调用；空对象授权访问公开上游。

```json
{
  "github": {"bearer": "upstream-access-token"},
  "legacy": {
    "basic": {"username": "alice", "password": "upstream-password"},
    "headers": {"X-Tenant-ID": "team-a"}
  },
  "local": {"env": {"API_KEY": "local-api-key"}}
}
```

Bearer、Basic 和 `headers.Authorization` 三者互斥。API key、Cookie、自定义 token
直接使用 `headers`。stdio grant 只能注入 `credential_env` 中声明的名称，不能携带
HTTP 凭据。文件是签发方输入，Gateway 不读取客户端的明文凭据文件。

```sh
chmod 600 credentials.json
uv run --locked --package kapy-mcp-gateway mcp-gateway issue \
  --config gateway.json --credentials credentials.json --subject alice --ttl 3600
```

命令仅向 stdout 输出一个 JWE，下游只保存这个 token：

```python
from fastmcp import Client


async def use_gateway(token: str) -> None:
    async with Client("http://127.0.0.1:8001/mcp", auth=token) as client:
        print(await client.list_tools())
        result = await client.call_tool("github.search", {"query": "FastMCP"})
        print(result)
```

## 聚合契约

| 上游组件 | 下游标识 |
| --- | --- |
| 工具 `search`，server `github` | `github.search` |
| prompt `review`，server `github` | `github.review` |
| resource `data://status`，server `github` | `mcp-gateway://github/data://status` |
| template `data://items/{id}`，server `github` | `mcp-gateway://github/data://items/{id}` |
| resource `urn:example:status`，server `github` | `mcp-gateway://github/urn:example:status` |

`.` 是合法 MCP 工具名字符。工具 schema、结果、错误标记、prompt 参数和资源内容
由 FastMCP 代理处理；URI 加上可逆外层，调用时还原上游 URI。不同 namespace 的
同名组件不会覆盖彼此。上游连接按请求建立和关闭，不保留跨调用的 MCP session 状态；
stdio 每次启动一个独立进程，退出请求上下文时关闭。客户端和上游默认保持同一协议模式，
旧版握手、sampling、elicitation、roots、logging 与 progress 使用 FastMCP 的代理转发。
后台 task 扩展不支持经过代理执行。
显式固定上游 `mode` 可桥接普通工具、资源和 prompt；两端协议模式不同时，
sampling/elicitation 等依赖特定协议模式的功能受 FastMCP 的协议限制。

每次查询使用独立 ProxyProvider，组件元数据不跨用户缓存。下游的 Authorization、
Cookie 和其他 header 都不会自动传到上游，只有配置或 grant 显式提供的 header 会发送。
已授权上游连接失败时 MCP 请求失败，避免把上游故障表现成空列表；整个列表要求所有
授权的上游可用。组件发现异常只返回失败的 namespace，Gateway 自身执行异常隐藏
内部详情，上游 MCP `isError` 返回值保持原样。
Gateway 配置在创建时复制，之后更改原配置对象不改变已创建的服务。

## Python 接口和自定义鉴权

```python
import os
from pathlib import Path
from mcp_gateway import GatewayConfig, TokenCodec, create_gateway, decode_key

config = GatewayConfig.model_validate_json(Path("gateway.json").read_text())
codec = TokenCodec(
    {"primary": decode_key(os.environ["MCP_GATEWAY_JWE_KEY"])},
    active_key_id="primary",
    issuer=config.issuer,
    audience=config.audience,
)
gateway = create_gateway(config, codec)
app = gateway.http_app(path="/mcp")  # 嵌入 ASGI 时由宿主运行 app 的 lifespan。
```

OAuth access token 可作为 `bearer`。需要自动刷新、client credentials、签名等流程时，
通过 `auth_factories={namespace: factory}` 注入可信 Python 代码。factory 接收
`HTTPUpstream` 与当前请求的 `UpstreamCredentials`，返回 `httpx2.Auth`。
它在每次创建上游连接时运行，可实现 `async_auth_flow` 发出刷新/交换请求；
适配器负责额外 HTTP client 的生命周期，避免跨用户共享可变 token 状态。

例如通过适配器生成自定义鉴权 header：

```python
from collections.abc import Generator
import httpx2
from mcp_gateway import HTTPUpstream, UpstreamCredentials, create_gateway


class CustomAuth(httpx2.Auth):
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        request.headers["X-Custom-Auth"] = self.secret
        yield request


def custom_auth(server: HTTPUpstream, grant: UpstreamCredentials) -> httpx2.Auth:
    secret = grant.custom["secret"]
    if not isinstance(secret, str):
        raise ValueError("Expected a string credential")
    return CustomAuth(secret)


gateway = create_gateway(config, codec, auth_factories={"github": custom_auth})
token = codec.issue(
    "alice",
    {"github": UpstreamCredentials(custom={"secret": "upstream-secret"})},
)
```

`custom` 只交给已配置的适配器，未知 namespace 或没有适配器的 custom grant 会被拒绝。
适配器负责 Authorization 时，grant 不能同时提供另一种 Authorization。
CLI 不动态加载适配器；需要此功能时使用 Python 入口。交互式 OAuth 授权由可信签发方
或业务适配器完成，Gateway 不提供 OAuth 登录页或公共 token 签发接口。

## JWE 与生命周期

使用标准 compact JWE，限定 `alg=dir` 和 `enc=A256GCM`，同时提供机密性与完整性。
此共享密钥模式中，持有密钥即可签发有效凭据。客户端持有 JWE 不代表持有签发密钥。
不接受仅用公共加密密钥生成且没有可信签名的封装。

载荷包含 `iss`、`aud`、`sub`、`jti`、`iat`、`nbf`、`exp` 和 `upstreams`。
严格验证 issuer、audience、时间、类型、算法和配置允许的上游；无凭据、篡改或过期
凭据返回 HTTP 401。默认有效期一小时，最多一天；Python `TokenCodec.max_lifetime`
可调整上限。token 最大 8192 字节，需让反向代理的 header 限额容纳它。
生产环境通过 HTTPS 传输，JWE 仍是可重放的 bearer credential。

这是无数据库的授权：grant 在有效期内持续有效，不能逐 token 即时撤销，也不会自动
更新封装的 access token。上游凭据刷新后可重新签发 JWE；更短 TTL 可缩小撤销窗口。
客户端更换 JWE 后重新建立 MCP session；session ID 与原 token 的独立 `jti` 绑定，
即使 subject 相同也不能用另一枚 token 复用 session。

Python key ring 可同时保留旧 key 和新 key，签发使用 `active_key_id`，解密根据受保护的
`kid` 选择。旧 token 到期后移除旧 key，或移除它立即撤销其签发的全部 token。
普通对象 repr/JSON 序列化遮盖 header、password、env 和 bearer，排除 custom；
`plaintext()` 是供可信签发或 inline 启动使用的显式凭据导出接口。

## 验证

完整测试在容器中执行；独立目录可直接安装并测试，不需要主应用环境：

```sh
docker run --rm --mount "type=bind,source=$PWD/packages/mcp_gateway,target=/work,readonly" \
  --workdir /work python:3.14-slim sh -ec '
  python -m venv /tmp/gateway-tests
  /tmp/gateway-tests/bin/pip install --quiet . pytest pytest-asyncio
  /tmp/gateway-tests/bin/python -m pytest -q -p no:cacheprovider tests
  '
```

已有 uv 开发容器并把工作区挂载到 `/work` 时，也可以使用锁定的 workspace 依赖：

```sh
cd /work
uv run --locked --package kapy-mcp-gateway pytest -q -p no:cacheprovider \
  -c packages/mcp_gateway/pyproject.toml packages/mcp_gateway/tests
```

静态检查从仓库根目录执行：

```sh
uv run --locked --package kapy-mcp-gateway ruff check packages/mcp_gateway
uv run --locked --package kapy-mcp-gateway pyrefly check -c packages/mcp_gateway/pyproject.toml
```

测试使用真实本地 HTTP/SSE MCP 服务和真实 stdio 子进程，无外部模型或业务凭据。
