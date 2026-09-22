# Shellctl

从 Dify 固定版本引入的 Go 服务端和 Python SDK，独立于 `kapy.*`。
上游仓库、commit 和路径映射由 [同步脚本](../../scripts/vendor_shellctl.py)
固定，并生成 [UPSTREAM.json](UPSTREAM.json)。上游许可证保存在 [LICENSE](LICENSE)，
Go 子依赖自带的许可证也原样保留。

```text
packages/shellctl/
  server/             # 完整 dify-agent-runtime Go 模块，包括原有测试和构建文件
  src/shellctl/       # Python SDK 和 DTO，保持上游 import 路径
  tests/upstream/     # 上游 SDK 测试
  tests/test_server.py # 本仓库的客户端／服务端集成验证
  pyproject.toml      # 本仓库的独立 Python 打包配置
  Makefile            # 本仓库的独立构建和测试入口
```

`server/`、`src/shellctl/`、`tests/upstream/`、`LICENSE` 和 `UPSTREAM.json`
由脚本管理，不要手工编辑。要升级上游版本，修改脚本中的 `UPSTREAM_COMMIT`
后重新运行；它会替换这些路径，移除旧版本残留文件，保留外围打包和集成代码。

```sh
python3 scripts/vendor_shellctl.py
# 已有 clone 时可复用；始终读取固定 commit，而非 clone 的当前工作区。
python3 scripts/vendor_shellctl.py --source /path/to/dify
```

Python 分发包名为 `kapy-shellctl`，模块名为 `shellctl`。根项目通过 uv workspace
依赖此包，`uv sync --locked` 后即可直接引用，不需要调整 `PYTHONPATH`。
包只依赖 HTTP 客户端和 Pydantic，不安装 Dify 后端。

```python
from shellctl import JobMode, ShellctlClient

async def example() -> None:
    async with ShellctlClient("http://127.0.0.1:8765") as client:
        result = await client.run("printf 'hello\\n'", mode=JobMode.STDIO)
        print(result.output)
        while not result.done or result.truncated:
            result = await client.wait(result.job_id, offset=result.offset)
            print(result.output)
```

SDK 自建的 HTTP client 由其上下文关闭；注入的 client 由调用方关闭。
退出 SDK 上下文不会终止远端任务。SDK 当前提供 job API，快照 API 仍使用 HTTP。

服务端需要 Go 1.26 编译，运行时需要 Linux、tmux 和四个 shellctl 二进制。
从仓库根目录执行：

```sh
make -C packages/shellctl build-server
PATH="$PWD/packages/shellctl/server/bin:$PATH" \
  packages/shellctl/server/bin/shellctl serve --listen 127.0.0.1:8765
```

服务保留上游默认值：状态数据位于 `$XDG_DATA_HOME/shellctl`，未设置时为
`~/.local/share/shellctl`；它拥有独立 SQLite，与 Kapy 主库无关。
服务端配置以 Go 实现为准，SDK 中保留的旧 Python 本地 runtime 路径辅助函数
不用于定位 Go 服务端。HTTP 服务重启后通过 tmux 和退出记录恢复任务；
终态任务默认保留 300 秒。详见 [上游服务契约](server/README.md)。

完整 Go 模块还包含与 Dify Agent Stub 通信的 `dify-agent` CLI；上述构建入口
只构建 shellctl 所需的四个程序。上游默认 `make build` 会向 Dify 后端写入
生成的 CLI help，因此在这里使用外层 Makefile。Go 模块路径保持原样，便于对照上游。

```sh
make -C packages/shellctl test-client
make -C packages/shellctl test-server
make -C packages/shellctl test-integration
```

集成验证使用临时 SQLite、专用 tmux socket 和本地 HTTP 端口，检查 SDK 的
stdio 输出、PTY 输入，以及 HTTP 服务异常重启后的任务恢复，最后关闭测试资源。
上游测试中依赖完整 Dify 仓库的 CLI help 快照检查可能跳过。
