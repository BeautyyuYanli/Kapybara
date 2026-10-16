# Agent Sandbox SDK

面向 Agent 的可组合沙盒集成 SDK，以 Rust 为核心。借鉴 OpenDAL 的统一接口、独立后端、显式能力查询，但不实现沙盒运行时、守护进程、队列或控制面。

这是独立 Cargo workspace，不依赖 `kapy.*`，也不替换本仓库当前的进程实现或 shellctl。首版包含 Rust 和独立的 Python/PyO3 绑定。

## 三层不是流水线

三层表示责任边界，不是强制 `Lifecycle → Connection → ProcessManager` 的调用链：

| 公共接口 | 职责 | 返回或标识 |
| --- | --- | --- |
| `Lifecycle` | `create/inspect/start/stop/delete/fork` | 后端名与不透明资源 ID |
| `Connection` | `exec`，等待短命令完成 | 字节 stdout/stderr、退出状态 |
| `ProcessManager` | `spawn/status/wait/output`、输入、信号、PTY、重连 | 后端原生进程 ID 与输出订阅 |

例如：

```text
DockerLifecycle ──资源引用──> DockerConnection ──注入──> Pueue
E2bLifecycle ──资源引用──> Envd ──实现──> Connection + ProcessManager
AgentSandboxLifecycle                  Sandboxd (Connection)
OpenShellLifecycle ──资源引用──> OpenShellConnection
Local 或 Ssh ──注入──> Pueue
```

Pueue 是包装任意 `Arc<dyn Connection>` 的适配器；envd 则直接通过原生 RPC 同时实现第二、第三层，不需要虚构一个下层 exec。生命周期层也不负责为任意资源猜测网络地址。`sandboxd` 的端点由调用方提供。

统一入口是 [`agent-sandbox-core`](crates/core/src/lib.rs) 的三个 trait、DTO、错误和能力模型，没有包含多个可选层的总控 `Sandbox` 对象。第三方适配器直接实现 trait；不需要插件注册、全局状态或配置 DSL。

## 集成与能力

| 后端 / Cargo feature | 生命周期 | 短命令连接 | 进程管理 |
| --- | --- | --- | --- |
| Local / `local` | 无 | 本机 argv 与显式 shell | 可组合本机 Pueue；没有隔离保证 |
| SSH / `ssh` | 无 | 系统 OpenSSH | 可组合远端 Pueue |
| Docker / `docker` | create/inspect/start/stop/delete | 原生 Engine exec，Bollard | 可组合容器内 Pueue |
| E2B / `e2b` | create/inspect/resume/pause/delete/native fork | 组合 envd | 组合 envd |
| agent-sandbox / `agent-sandbox` | Kubernetes Sandbox CR 生命周期 | 独立组合 sandboxd 或其他连接 | 无 SDK 自建进程服务 |
| OpenShell / `openshell` | 原生 gRPC 生命周期 | 原生 gRPC exec | 不提供通用进程管理 |
| Pueue / `pueue` | 无 | 复用注入的连接 | 官方 v4 CLI / pueued |
| envd / `envd` | 无 | 原生 Start 并收集结果 | 原生 ConnectRPC 进程接口 |
| sandboxd / `sandboxd` | 无 | 原生 gRPC Execute | 不提供连接外的进程句柄 |

Docker、agent-sandbox、OpenShell 的 `fork` 返回 `Unsupported`，不以重建、复制文件或 Docker commit 冒充内存 fork。E2B 使用原生 `fork`，一次返回一个子沙盒。

### 第三层的独立能力

| 能力 | Pueue | envd |
| --- | --- | --- |
| stdin | UTF-8、无 NUL 的有限输入 | 字节 stdin / PTY 输入 |
| close_stdin | 不支持 | 非 PTY 支持 |
| PTY / resize | 不支持 | 支持；输出是 `terminal` 合并终端流 |
| 信号 | SIGINT / SIGTERM / SIGKILL，由原生任务状态决定 | SIGTERM / SIGKILL；SIGINT 返回 Unsupported |
| 脱离输出订阅继续运行 | 支持 | 支持 |
| 按 ID 重连 | 原生任务仍保留时 | 原生 PID 仍可连接时 |
| 历史输出 | Pueue 仍保留任务及日志时 | 不承诺完整历史输出 |
| 实时输出 | 首版不提供，完成后返回合并日志 | 原生输出流 |
| daemon 重启后恢复运行进程 | 不承诺 | 不承诺 |

运行中、可重连、日志可回读、daemon 重启恢复是不同能力。对具体实例调用 `capabilities()`；不要仅根据后端名假定所有操作可用。

## 命令与错误契约

- 默认传显式 argv，只有 `Command::shell` / Python `.shell()` 使用 `/bin/sh -c`。SSH 和 Pueue 所需的 POSIX shell 转换逐项引用参数，不直接拼接用户命令。
- SSH 远端和 Pueue 的 CLI/daemon 执行环境需要 `env`，以及支持 `-n 0 --` 的 `/usr/bin/nice`。该固定原生 helper 在环境覆盖后保持优先级不变，并保留包含 `=` 的可执行路径和非 POSIX 环境键；任务 PATH 不影响 helper 定位。
- `cwd`、`env` 是请求字段，`stdin`、stdout、stderr 都使用字节。PTY、Pueue 合并输出分别标为 `Terminal`、`Combined`，不伪装成分离 stderr。
- 第二层 `exec` 等待完成，不返回后台任务 ID。Rust 为 async I/O，Python 为释放 GIL 的阻塞调用；输出收集到内存，不适合无限输出。
- 非零退出码属于正常 `ExecResult`，不是传输异常。信号退出可以没有数字退出码；Pueue 的 Killed 结果不包含精确退出码或信号。
- SDK 错误分为 InvalidArgument、Unsupported、NotFound、Conflict、Permission、Timeout、Transport、Backend，并带 backend/operation。普通错误不回显供应商响应正文、命令输入或凭据。
- `ExecRequest.timeout=None` 表示不设置整条命令的 SDK deadline；连接建立和原生控制请求仍可以有适配器级超时。零超时无效。
- 超时/断连不等于远端进程已停止。Local 在取消时清理 Unix 进程组；SSH 只保证结束本机客户端；Docker 关闭 exec attachment，不保证终止 exec；envd 关闭订阅而不隐式发送 kill；OpenShell 的原生执行超时由服务端处理；sandboxd 取消请求的清理行为由服务端决定。
- SDK 不自行重试 create/fork/exec/spawn 等不确定结果。HTTP 客户端禁用重试及重定向；kubectl 仍保留 client-go 对明确 `Retry-After` 响应的内置行为，创建始终使用明确的 `metadata.name`，同次重放不会产生另一个名称的 CR。调用方不得盲目重做超时的副作用操作。
- 资源始终显式 `delete`。丢弃 Rust/Python 客户端、输出订阅或网络连接不自动销毁沙盒。

## 后端前提和边界

### Local / SSH / Docker

Local 在本机继承环境，并应用请求的环境覆盖；它不是沙盒。SSH 需要 POSIX 远端及系统 `ssh`，使用 BatchMode，保留用户配置、SSH agent 和 known_hosts 校验，不添加跳过主机校验的选项。OpenSSH 的退出 255 无法与远端命令退出 255 完全区分，统一报告 Transport；需要无此歧义的协议时使用其他连接。

Docker 使用调用者提供的 Engine 客户端，或 Bollard 的默认连接配置。不会自动 pull 镜像。create 返回停止的容器，start 需要显式调用；stop/start 不保留进程内存；delete 强制移除容器，但不会代替调用方清理外部卷。隔离、资源限额、挂载和网络设置来自 `DockerConfig.host_config`，SDK 不为它们作安全兜底。

Docker exec 观察原生完成后取消未被消费的 stdin，仍读完输出再返回退出码；进程仍在运行时的输入/传输错误不会被忽略。

### Pueue

调用方安装 Pueue v4，并管理 pueued、分组和并发策略。SDK 不安装、启动或关闭守护进程。CLI 必须与 daemon 位于相同执行环境，并设置 `client.read_local_logs: true`，才能通过 `pueue follow` 保留任意字节；原生远程日志协议会进行文本解码。

首版在任务完成后读取合并日志，以适应第二层的“命令完成后返回结果”契约，不宣称实时 streaming。`send_input` 仅支持 CLI 可表达的 UTF-8、无 NUL 输入，不提供关闭 stdin。清理原生任务或日志后，SDK 没有自己的副本；daemon 重启后的原生任务保留不等于进程恢复。

任务 `env` 在安全引用的队列命令内应用，不修改 Pueue CLI 的 PATH、HOME 或配置环境。

### E2B / envd

E2B 控制面使用 v2 create/connect、inspect、full-memory pause、delete 和原生 fork。create 一般返回运行中沙盒；stop 是原生内存暂停，start 是 resume，不等同 Docker stop/start。权限、模板与套餐是否允许操作仍由服务端决定。

`E2bLifecycle.envd(resource)` 只读 inspect 发现 envd token/domain，然后构造独立客户端，不隐式 resume。也可以直接构造 `EnvdConfig` 连接独立 envd。API key 与 envd access token 分开；E2B 代理路由通过 Sandbox ID/port headers 传递。

envd 的 `Start` / `Connect` 是 ConnectRPC，而不是假设为 gRPC。字节输出、stdin、信号和 PTY 使用固定的上游 protobuf schema。`status` 使用原生 List；进程不在 List 中时返回 NotFound，不能推断历史退出码。完成结果从 handle 的 Exit 事件获得，输出流在 Exit 前断开返回 Transport。重连遵循 envd 的保留窗口，不是持久日志系统。

短命令结束后，SendInput/CloseStdin 可能因原生进程已移除而返回 NotFound；只有同时取得有效 Exit 才将其视为正常完成。等待这类竞争的完成事件受控制请求超时限制，其他输入错误仍然报错。

### Kubernetes SIGs agent-sandbox / sandboxd

生命周期通过系统 kubectl 操作 `agents.x-k8s.io/v1beta1` 的 Sandbox CR，复用 kubeconfig/context/exec credential provider。PodTemplate 与 PVC 使用供应商原生 JSON 配置，交由 Kubernetes 校验。start/stop 只修改 `spec.operatingMode`，不等待 controller 完成；inspect 使用当前 generation 的 observed conditions，不能把 desired mode 当作实际状态。

Suspended 删除 Pod、保留 CR 和卷，不保留内存。sandboxd 是独立原生 gRPC Connection，不从旧 HTTP router 猜地址。其 Execute 没有 stdin 字段，非空 stdin 返回 Unsupported；连接绑定的 Start 缺少脱离/重连语义，因此首版不冒充第三层。

### NVIDIA OpenShell

使用 OpenShell v0.1.4 原生 gRPC，显式提供 gateway endpoint、workspace，以及可选的 bearer、CA、mTLS 证书/私钥。HTTPS 验证服务器证书，不读取 CLI 登录态，不自动建立代理隧道。远程网络上的明文 HTTP 不适合传凭据。

创建可以使用原生 `SandboxSpec` 或命名 workload template，二选一。exec 先只读查询 owner，转发路由 hint，然后调用原生 exec；不在不确定失败后重试。执行使用非 login shell 模式，保留 argv 和分离的字节输出。普通 exec 不等于主进程 connect：主进程的有限 replay 不作为任意进程历史接口。delete 表示服务端接受请求，不保证资源已同步消失。

## Rust 使用

从本目录执行；要求 Rust 1.89+，验证使用 Rust 1.99。核心不依赖任何供应商 SDK，适配器默认只开启 `local`，使用 feature 按需启用：

```toml
[dependencies]
agent-sandbox-core = { path = "../agent-sandbox-sdk/crates/core" }
agent-sandbox-adapters = { path = "../agent-sandbox-sdk/crates/adapters", default-features = false, features = ["docker", "pueue"] }
```

可编译的组合示例在 [`examples/`](examples/)：

```sh
cargo run -p agent-sandbox-adapters --example local_exec
# 需要已存在的镜像、Docker daemon，以及镜像内可作为主命令运行的 pueued。
PUEUE_IMAGE=your-pueue-image cargo run -p agent-sandbox-adapters --features docker,pueue --example docker_pueue
# 创建并显式删除真实的计费资源，仅在准备好云凭据后运行。
E2B_API_KEY=... cargo run -p agent-sandbox-adapters --features e2b --example e2b_envd
```

## Python 使用

构建与使用详见 [`bindings/python/README.md`](bindings/python/README.md)。示例只使用本机，不创建服务：

```python
import agent_sandbox_sdk as sdk

result = sdk.local().exec(["printf", "%s", "hello\n"], timeout=5)
assert result.success
assert result.stdout == b"hello\n"

# pueued 由调用方预先启动，SDK 只连接它。
processes = sdk.pueue(sdk.local(), config_file="/path/to/pueue.yml")
handle = processes.spawn(["python3", "-c", "print('job')"])
while (event := handle.next_event(timeout=10)) is not None:
    if event.exit is not None:
        break
    print(event.channel, event.data)
```

## 生成与验证

协议来源与固定版本见 [`protocols/README.md`](crates/adapters/protocols/README.md)。上游文件由 `scripts/update-protocols.sh` 按固定版本下载并核对 SHA-256；Rust 通过 vendored protoc 在 Cargo `OUT_DIR` 生成。不手工改下载文件或生成代码。

流程测试需要真实进程、SSH、Pueue、kubectl、OpenSSL 和 Docker socket，必须在容器中运行，不以宿主机 skip 作为通过。构建测试镜像不会创建 Kubernetes 或云沙盒：

```sh
docker build -f Dockerfile.tests -t agent-sandbox-sdk-tests:latest .
docker run --rm \
  -v "$PWD:/workspace:ro" \
  -v agent-sandbox-sdk-target:/target \
  -v agent-sandbox-sdk-registry:/usr/local/cargo/registry \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e CARGO_TARGET_DIR=/target \
  -e AGENT_SANDBOX_TEST_IMAGE=agent-sandbox-sdk-tests:latest \
  agent-sandbox-sdk-tests:latest
```

Docker socket 具有宿主机管理权限，只对可信代码运行。测试会创建并删除隔离的 Docker 容器、sshd 和 pueued；不启动 Kapy。脚本执行格式检查、各 feature 编译、Rust 测试/Clippy/rustdoc、构建并安装 Python wheel，再运行 Python 绑定测试。Python envd fixture 使用同一固定 schema，由 grpcio-tools 在临时目录生成协议类型；它不是 wheel 的运行时依赖。

Local、SSH、Docker、Pueue 使用真实运行时验证；E2B/envd、sandboxd、OpenShell 使用固定 schema 的本地 HTTP/ConnectRPC/gRPC fixture；gRPC 另验证真实 TLS 的 CA/主机名校验和必需的 mTLS 客户端身份；agent-sandbox 使用真实 kubectl 对接本地 Kubernetes discovery/CR fixture。后者验证协议与参数边界，不代表已通过真实云服务、Kubernetes controller 或 OpenShell 部署的端到端认证测试。
