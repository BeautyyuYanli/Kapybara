实现一个如下设计的 agent 系统  

总体上基于 python 实现，开发工具栈使用 uv 、ruff、pyrefly，按需可能引入（但不必须） rust、maturin/pyo3。基础库使用 pydantic pydantic-settings logfire httpx2 fastapi 这类的活跃流行库。sqlmodel 和 sqlalchemy 可以用于管理数据库连接、定义、迁移和写入，但在读出时应该绕过 validation 以避免不必要的性能退化，仅做类型 hint 即可。kv 使用 valkey 客户端。遵循 python 3.14 最新的语言特性和类型系统，强类型优先，但不是必要的，使用 anyio 和 uvloop 并合理地使用 free thread，合理的情况下尽可能异步并行。可以使用 websockets 和 starlette 的 websocket。  
这个 agent 的控制面与执行面分离，在控制面上应存在一个 server，用于管理 pydantic-ai 运行中的 agent loop 及其 session 状态等。在执行面上应存在一个可以用 uvx 调用的管理工具，名叫 kapy。当前架构下 kapy 在执行面机器侧上运行一个 kapy server ，一方面管理执行面的机器，另一方面和控制面保持 websocket 持续连接，包括自动重连、idle 时可以断线节约资源，之类的，统一用 jsonrpc 交互。  

执行面机器侧，kapy server 接受来自控制面的请求，工作就是在执行机器上运行命令并返回结果。遵循 XDG 规范。它应该有一个进程管理器，负责跟踪去运行的命令。有两种工作模式，一种是基于 stdio 的纯粹代执行，可能类似 ssh 一样，完成 request 返回 response。另一种复杂一点更接近 PTY，首先对于下辖进程来说是个 PTY，支持交互，那它对于控制面看到的接口，包括输入去运行命令，阻塞等待到命令输出停止或者超时，但超时也不是杀掉进程，还是可以再等待一次，或者做输入动作去交互，包括卡住的情况下也很可能要输入动作去交互，当然也支持输入 control C 去打断。支持多个 PTY 去下辖进程，提供给控制面的接口也支持杀掉一整个 PTY 这样去杀掉它下辖的整个进程树。对于控制面来说这个 PTY 模式的接口就是提供给 agent 的一批内置 tools。在执行面机器侧我建议用 sqlite 去管理状态，放在符合 XDG 规范的地方。除了进程管理器之外，这个执行面机器侧还有个文件管理器，那主要就是让控制面从机器侧 pull 文件、push 文件。这个 pull push 既要支持直接走已有的连接数据传输到控制面，也要支持控制面发 presigned URL 让机器侧去上传/下载（这块可以用 opendal 做）。执行面的机器侧还有一套 control 命令是对控制面的一些功能的 proxy，那用法会类似是 kapy control xxx ，默认去连接这个本地 kapy server，因为有 proxy 所以鉴权之类的问题有一部分就在这里面解决了，控制面可以知道请求是来自这个机器的。进程管理器和文件管理器都要健壮一些，不要被大输出/大文件打爆了，如有必要可以调研一些可信的库去用。  进程管理工具 PTY 模式当然也是有缓冲区窗口的，超出窗口的 output 就自动截断了，设 8192 Bytes 吧。stdio 模式就不设这个限制了保持简单。

那执行面的控制侧呢，其实就是控制面内嵌的 kapy server 的客户端，一方面提供进程管理器和文件管理器的接口给控制面的 agent 去调用，另一方面接收 kapy control xxx 的命令，去调用控制面的一些能力。  

控制面的角度，首先我们谈 agent loop。它基于 pydantic-ai 去做，提供的 tool 包括我们刚才说的进程管理器。除此之外有一个 read media tool，给 path，通过刚才说的这个文件管理器去读媒体文件，传到大模型调用（注意大模型调用不保证支持哪些媒体文件，所以出错的话要把模型侧的报错信息作为文字形成 tool response，而不是让报错的多模态 tool response 弄坏 loop）。然后有一个接口化插件化的可自定义参数/description 的 tool 插件管理器，要求内部实现是把 tool 参数转换成相应的命令脚本。然后进入标准的进程管理器 tools 的处理流程。第一个插件是 apply patch tool 里面包的是 https://github.com/BeautyyuYanli/codex-apply-patch ，用它的 skill.md 作为 description。还有一个 wait tool ，它只有一个 list 参数就是 wait for，一个数组描述 wait 等待事件的编号。注意 agent 的基础 prompt 和 tool description 要站在 agent 的角度去写，描述清楚行为细节而不暴露技术细节。 最终 agent loop 结束的时候我们锁它进入 waiting 状态，因为它就算不用 wait for tool 也会有一个默认监听的 waiting 就是输入 channel，我们后面细说。

然后 agent 是以 session 为单位的。session 就是一个连续的 agent 上下文和相应的执行层环境嘛。不同的 session 当然可以并发并行，但一个 session 内部是串行的，它提供两个缓冲区来保存输入，一个是 steer，就是说 agent loop 工作的间隙去把这个缓冲区的信息插入进入然后继续 loop；另一个是 queue，就是说进入 wait 状态后再用这个缓冲区的信息发进去开启新一轮 loop，当然这里肯定也包括没来得及发的 steer 缓冲区。输出呢，一方面提供缓冲区供实时消费实时输出，另一方面就是要整理好 delta 了变成历史记录以供持久化和回放，比方说回放完了再进入实时输出的信息，这里面当然有一些 cursor 系统之类的。注意 session 是逻辑隔离不是落到存储层的物理隔离，所以所有涉及 session 内部运算的操作，都需要程序式地转换一下去筛选那个 session。

在 session 之上的控制层，这一层就要提供接口给前端的交互界面了。注意这里说的前端不只是 web，而是说和用户交互的界面，的抽象，通过这一层的接口去操作 agent session。这里也是有双向的逻辑，既包括用户主动向 session 推 prompt （steer or queue），也包括 session 主动向用户推更新状况。具体实现用 poll 还是 wait 你看着办吧，有需要的话两种都做也可以，自己做好缓冲区状态管理什么的。然后还有就是 session 的管理接口，创建 session 删除 session 什么的。

这里有意思的地方是，前面不是说了执行面会 proxy 一些控制面功能吗，session 之上的控制层接口，第一个接入者就是执行面机器侧的这个 cli。这里适合设计成异步的，就是如果用 cli 创建 session 或者往 session 里发 prompt，那 cli 会返回 session id 和 waiting id，接下来可以选择用 cli 去读 session
 id 的 output，也可以选择 wait_for 这个 waiting id 。从 session 管理的这一面的角度讲，如果创建 session / 往 session 里面发 input 的时候有给 waiting id，那就是这个 session 自己进入 waiting 状态的时候，算是完成了，用来源的 waiting id 去发 event 唤醒前序的等着它的 session。换句话说这里是一个递归操作，一个 session 用这一套 waiting 系统去操作另一个 session。

说到这里我们引入了这个 waiting 系统了，那它就是一个异步事件系统嘛，一个 subscriber 监听一个 waiting id，这个 waiting id 就是一个 channel。这里可能是 MPMC 系统，一个 producer 往里发事件会唤醒所有的 subscriber 让它们去消费，这里唤醒完就算消费了，剩下的可靠性就 agent session 自己去保证；如果投递事件的时侯还没有监听者进来，就滞留在队列里，等到有监听者的时侯立即投递。那在这个模型里 session 的 input/output 其实就是有一个默认 channel/waiting id 去接收 input 或发送 output，完了再 subscribe 别的 waiting for id。这个 waiting for id 是可以多次投递多次把 subscriber 唤醒的。producer 投进去的时侯可选 steer 或者 queue，如果来自用户界面就是用户界面自己选，如果来自其它渠道默认就 steer。还有一个 agent session 本身既是其默认 channel 的 producer 也是 subscriber，它进入 wait 状态的时候把 payload 发进 channel，那当然它自己是不会被自己的信号给再次唤醒的；前端层的话就只是 producer 不是 subscriber，因为读出这部分走 session 接口，而不是在这一套异步事件体系里。这个异步事件系统本身反正你按 best practice 去写保证它的可靠性就好了。

前端么当然也是接口化插件化的，除了 kapy cli 之外的第一个对接插件应该是 telegram，用 bot api，一个 chat id 支持 thread id 的话就用 thread 对应一个 session，不然的话就是这个 chat 本身就是一个 session。因为 telegram 没有复杂的前端交互，所以一些设置的东西就用 tg command 去设置吧，创建新 session 就用这个已保存的 session。

说回 session 接口，这里面还有一个重要成分就是历史记录的查询，我这里的想法是历史记录就存主数据库 postgres，然后提供一个按 session 筛选过的子视图去给 agent 查询。比方说 cli 运行一个命令写 SQL 语句，在自己的历史里运行查询 SQL，这个 SQL 就是在按 session 筛选过的子视图里运行的。这里你自己做一下技术选型，至少应该支持子串匹配和全文索引关键词匹配（多语言优先）。

有了这个历史记录管理的工具的话，放在 agent loop 里的历史上下文就可以做压缩了。我预想的压缩策略是 model 有它的 context window 的值嘛这个值是可变的，按一个比例设置阈值， 70% 吧。然后有三级压缩策略，0 级是原始信息，1 级是所有的 tool call 只保留 call 的内容不保留 response 的内容，2 级是只保留两次 waiting 状态之间的 input 和进入后面这个 waiting 状态时的output ，再然后就是丢弃了。当触发阈值的时候，保留最新 10% 左右的仍不变处于 0 级，10% 开外的 0 级降到 1 级，1 级降到 2 级，2 级就丢弃了。

说到 session 和 machine 的映射关系了。一个 session 是支持多个 machine 的，所以那些 tools 和 machine 有关的话应该有参数指定是哪个 machine。当然可以有一个默认 machine 不指定的话就是它了。一个 machine 可以去运行多个 session，所以前面说的这个 proxy 它虽然知道请求来自哪个 machine，但还要有个 token 传递去表达请求来自哪个 session。每个 machine 都应该在符合 XDG 规范的地方新建一个文件夹作为这个 session 的 working directory 或称 cwd。

接下来我们说到 skill。我们要有个 skill 管理系统，它和 session 管理系统算是平级的。支持 skill 的 CURD，也就是符合 agent skill 标准的压缩包就行。注意有个接口是去获取 skill 的 id 和 description，这个很重要它就是默认在 session 创建的时候会去获取全量的 skill description 数据，放在最初的 agent instruction 里面。这个接口还要至少支持子串匹配的筛选获取，后面 agent 用基于 proxy 的 cli 去获取新增的 skill。另外就是 agent 用的 proxy cli 可以去上传新创建的 skill，可以获取一个 skill 的 SKILL.md 全文，可以下载一整个 skill 文件夹去用里面的东西。

综上，基建你自己选型吧，开发环境可以用 docker compose 搭建。开发系统使用的 agent 模型我们暂定用 gpt-5.6-luna ，API base 和 key 我后面告诉你。最终应该交付出一个符合上述条件的 agent 系统，并且有 telegram 集成。