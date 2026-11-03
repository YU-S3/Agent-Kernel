# Agent Kernel v1.0 — 最终架构与实施合同

> **状态：Architecture / ABI v1 Freeze — 可交付编码，未宣称已经实现。**
>
> **依据**：2026-10-31 `plugin-foundation-feasibility.md`、2026-11-02 初版 `AgentKernel_v1_Architecture_RFC.md`，以及本轮关于入口、显式绑定、跨语言、错误监督的确认。
>
> **范围**：定义 Python 为主的通用插件 Kernel 及其跨进程合同；可运行任意插件应用，但不预设必须是 Agent。先在 `SkillCompiler-PluginLab` 独立副本实现，保持原项目及实验不变。
>
> **权威顺序**：本文规范性条款优先于早期 RFC 的示例；示例配置/API 为目标接口，不代表已有 `agentlab` 已支持。

## 0. 核心结论与边界

**Kernel = 统一 Plugin ABI + PluginHost + Supervisor + Control。**

- **PluginHost**：装载插件定义，创建实例，解析并显式绑定依赖，发布方法句柄，保证装配原子性。
- **Supervisor**：创建和监督通用执行活动、作用域、跨进程 Worker；处理取消、故障传播、资源回收。
- **Control**：对获得授权的执行实体实施权限、隔离和可强制执行的系统资源硬限制。
- **Kernel ABI**：所有插件共有的装载、调用、通信、身份、错误与生命周期合同；**不是第四套 Service Registry**。

**不进入 Kernel**：Agent Loop、Model Connector、Tool Executor、Workflow、Session Store、Context Builder、Memory、Reflection、Planning、Skill/Evolution、Benchmark、模型重试、Token/费用/对话步数预算、模型专用 API 适配。它们都是普通插件。Kernel 不对插件按“模型/工具/Agent”分类。

**总体标准**：新增普通能力、工作模式、算法或完整外部 Harness 时，若遵守本 ABI，不修改 Kernel；只有所有插件共有的底层运行语义确需变化时才升级 Kernel ABI。

## 1. 术语、身份与关系

| 对象 | 含义 | 身份 / 所属 |
|---|---|---|
| `PluginDefinition` | 已安装插件包的特定可执行定义 | `(plugin_id, version, digest)`；digest 含可执行代码与清单身份 |
| `PluginInstance` | 该定义按配置创建的运行实例 | `instance_id` 唯一；相同定义/配置可有多个实例 |
| `Execution` | 一次公开方法调用或独立受监督活动 | `execution_id` 唯一；包含父调用、结果/取消/故障 |
| `Scope` | 资源、权限和生命周期的归属边界 | `scope_id`，严格父子归属树 |
| `Handle` | 经验证、可失效的实例/执行引用 | 只可引用已授权实例和导出方法 |
| `Composition` | 被选定的包、实例、绑定、启动策略与可选默认入口 | `composition_digest`，启动装配时生成不可变锁 |
| `Invocation` | 用户/上层请求执行某个具体入口 | 有效 `(instance_id, export_name, input)` 及独立 Execution |

以下三种关系**绝不能混同**：

1. **静态绑定图**：`A.helper -> B`；定义依赖，允许多个消费者共享 B，可含纯引用环。
2. **Scope 归属树**：A 和 B 不因调用而自动成为父子；只有资源拥有者负责释放。
3. **运行时调用图**：一次 Execution 可能调用、并行或回调其他插件；由插件代码决定，不由 Kernel 解析为 Workflow DSL。

实例 ID 是唯一运行身份，不等价于包+配置哈希：两个相同配置的实例仍然是两个实例。Composition 哈希用于冻结定义和连接；Execution ID 用于每次调用追踪。

## 2. 唯一装载规范：Manifest 与 Composition

### 2.1 Manifest（所有插件同一规范）

```yaml
id: example.basic-loop
version: 1.0.0
kernel_api: 1
loader:
  adapter: python
  target: example_basic_loop.plugin:apply
requires:
  model: [infer]
  tools: [execute]
exports: [run]
config_schema: ./schemas/config.schema.json  # 可选、声明式 JSON Schema
requested_permissions: []                        # 可选，仅为申请，不产生授权
```

字段合同：

- `id + version + digest` 由安装/锁定阶段解析到**唯一字节身份**；同版本不同 digest 若未显式重锁定则失败。
- `kernel_api` 是公共装载 ABI 的代号，**不是针对每个方法的接口版本**。专业方法只依赖包锁定版本和名称；更严格的业务合同由插件自带测试/Schema 实现。
- `loader` 是通用**技术装载器**信息，不是业务 `kind`。内置 `python` 可同进程或在通用 Python Worker 内运行；`jsonrpc_stdio` 启动提供同一 ABI 的外部进程。外部命令由宿主直接执行参数数组，不经过 Shell；相对路径以已锁定的插件包根目录解析，Worker 只继承显式批准的环境变量和凭据。
- `requires` 是局部依赖**槽位**及必须存在的导出方法名；不填写全局 Provider 选择逻辑。
- `exports` 是声明的公开方法名；`apply()` 必须通过 `ctx.expose` 实际注册，装配后逐个核实。
- 可选 `config_schema` 优先是包内**声明式 JSON Schema**，由 Host 安全校验；Kernel 不知道 `protocol/model/base_url` 等业务字段。不能为了检查不可信插件配置，让 Host 直接 import/执行它提供的 Python 校验代码。若遗留插件必须使用代码校验，只能在已经建立的 Worker 隔离边界内执行，并在对外 READY/业务调用前报告结果。
- `requested_permissions` 仅表明需求；是否获准由宿主/Composition 的 Control 策略决定。

外部语言/完整 Harness 也不需要新的插件类别，只更换技术装载信息：

```yaml
id: example.external-harness
version: 1.0.0
kernel_api: 1
loader:
  adapter: jsonrpc_stdio
  command: [node, ./dist/agentlab-worker.js]
requires: {}
exports: [run, resume, cancel_session]
```

这里的 `run/resume/cancel_session` 是此插件作者选用的**普通导出名**，Kernel 不赋予其 Agent 专属含义。对于已存在的 CLI/SDK，应由薄包装 Worker 实现统一 ABI；不要求原生 CLI 自己识别 ABI。

### 2.2 Composition：显式实例、版本与绑定

```yaml
kernel_api: 1
composition: demo-agent
packages:
  - {id: example.basic-loop, version: 1.0.0, digest: "sha256:<locked>"}
  - {id: example.model, version: 1.0.0, digest: "sha256:<locked>"}
  - {id: example.tools, version: 1.0.0, digest: "sha256:<locked>"}
instances:
  model_ds:
    plugin: example.model
    config: {model: "<name>", base_url: "<url>", api_key_env: DEEPSEEK_API_KEY}
    execution: {isolation: process, max_inflight: 8}
  tools_fs:
    plugin: example.tools
    execution: {isolation: process, max_inflight: 4}
  agent:
    plugin: example.basic-loop
    bindings: {model: model_ds, tools: tools_fs}
    execution: {isolation: process, max_inflight: 1}
entry: {instance: agent, method: run} # 可选默认入口；不是 Workflow 类型
control:
  max_wall_seconds: 300
  network: restricted
```

`<locked>/<name>/<url>` 是示例占位符，不是可直接执行的配置；打包器/安装器应生成实际锁定值。`execution` 和 `control` 属于宿主通用运行/授权声明，不使 Model/Tool 变成内核专属类型。

- 每个 `requires` 槽位必须显式绑定到**一个**实例，不能运行时自动发现/按优先级选择 Provider。
- 同一 PluginDefinition 可有多个 Instance；可以同时绑定多个 Provider 实例，但**每个具体槽位唯一**。
- 所有**已声明启用的实例**必须通过包/配置/绑定/导出验证，避免把破损的闲置实例静默隐藏；具体 Invocation 从入口检验其可达依赖及准备状态。
- 动态创建使用已批准的实例模板/定义；不能通过运行时参数绕过锁定和权限。

### 2.3 入口合同（对前版的正式修正）

**没有必需的 `Workflow` 插件或名为 `run()` 的内核方法；执行入口就是一个普通插件实例导出的函数。**

- `host.load(composition)`：装配，无须入口；允许纯服务宿主。`on_ready` 背景工作也必须登记为受监督的 Execution。
- `host.invoke(instance, export, payload)`：显式调用该实例的某个公开导出；Composition 无需默认 `entry`。
- `host.run(composition, payload)`：使用 Composition 中**可选的唯一默认 `entry`**。
- CLI `kernel run profile.yaml --entry agent.run`：显式入口优先，且写入当次 Execution 记录；不修改锁定的 Composition 身份。科研运行的固定启动参数一并写入运行身份。
- 未提供显式入口且 Composition 无默认入口，报 `COMPOSITION.ENTRY_MISSING`；入口目标或方法不存在分别报 `COMPOSITION.INSTANCE_MISSING` / `COMPOSITION.EXPORT_MISSING`。多个入口只能通过多次独立 Invocation 调用，Kernel 不自动挑选。
- 入口就绪 ≠ 任务成功。Kernel 只能证明装配有效、方法存在且授权可调用；实际业务的完成性和返回结果由应用插件决定。

**因此 Kernel 是可独立加载的，但不会在未装载执行插件时自动成为 Agent。** BasicLoop 是以后安装的普通插件，既不是 Kernel 的强制依赖，也不是唯一能启动业务的插件。

## 3. PluginHost 的装配事务与生命周期

### 3.1 标准装配状态

`RESOLVED -> ALLOCATED -> REGISTERED -> LINKED -> READY -> DRAINING -> DISPOSED`，异常为 `FAILED`。

装配步骤：

1. **Resolve/Verify**：校验 Manifest/锁/装载器、配置 Schema、所有必需槽位/目标实例、声明导出名、Control 能力条件；不调用业务入口。
2. **Allocate**：创建 Scope、实例 ID、失效可控的 Handle；准备 Worker 与授权边界，尚不调用业务方法。
3. **Register (`apply`)**：每个插件只注册 `expose`、`on_ready`、`on_cleanup` 和自己的本地资源；不得在此阶段依赖调用其他插件。未就绪调用返回 `PLUGIN.NOT_READY`。
4. **Link/Validate**：注册全部实例后，校验实际导出与 `requires` 对应，完成显式绑定。若失败，整体回滚。
5. **Ready**：运行有限时的 `on_ready(startup_context)` 回调；它可以将后台任务登记为待启动的受监督 Execution，但这些任务**在全局 READY 后才调度**。`on_ready` 不得等待其他插件的业务方法结果，不得启动未登记的永驻任务。需要跨插件初始化时，在 READY 后由普通入口/工作流显式调用，不增加 Kernel 专用依赖调度规则。
6. **Dispatch**：所有启用实例就绪后，才受理普通入口调用；调用阶段由 Supervisor 负责。

原型当前**拒绝依赖环**，而最终规范允许**纯引用环**：通过两阶段注册→链接来完成。需要保留“禁止 `apply` 阶段调用依赖”的约束；不是允许任意初始化相互等待。`on_ready` 互等或运行时递归死锁会因通用截止时间/取消而失败，不能据此宣称可无条件安全支持循环业务调用。

`on_ready` 回调失败按 `PLUGIN.START_FAILED` 处理，回滚装配。回滚逆序清理每个已创建的资源；记录第一错误和清理错误列表，不留下可被外部调用的半活跃 Handle。

### 3.2 插件实例、调用、归属与热替换

- 实例的配置状态私有；共享实例由共同上级或服务 Scope 持有。消费者退出只撤销它自己的 Handle，不销毁正在被其他消费者使用的共享服务。
- 任何 Execution 归属明确的 Scope；取消一次 Execution 不影响同级无关 Execution，不自动卸载 Provider。
- `ctx.spawn_instance(template, ...)` 必须引用预授权模板/定义；新子实例获得独立实例 ID 和子 Scope，权限不超过创建者授予的上限。若需要跨越当前 Scope 创建持久服务实例，必须由上级授权的宿主 API 执行。
- 新包安装和开发态重载只影响新的装配/实例；已有调用不被悄悄替换。Drain 时停止接受新调用，完成或取消在途调用，按拥有关系释放。
- 正式科研 Run 冻结插件代码、配置、绑定和有效入口；变更必须形成新组合或派生 Run，不能冒充旧 Run 恢复。

## 4. 统一 PluginContext / 调用 ABI

以下是**约束级伪代码**，实现可用 Python `Protocol`/SDK，但跨语言 Worker 必须提供相同的对外语义。

```python
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

# 公开调用边界为 JSON-safe Value 或受授权 ResourceRef；
# 可信同进程原生对象须显式标记 local_only。
Value = Any

class CallContext(Protocol):
    execution_id: str
    scope_id: str
    # 由 Kernel 建立并验证：caller_id, parent_execution_id,
    # deadline, cancellation, tracing metadata

class PluginContext(Protocol):
    instance_id: str
    scope_id: str
    self_handle: "InstanceHandle"  # 自身受控 Handle
    def expose(self, name: str, fn: Callable) -> None: ...
    def on_ready(self, fn: Callable[[CallContext], Awaitable[None]]) -> None: ...
    def on_cleanup(self, fn: Callable[[], Awaitable[None]]) -> None: ...
    async def call(self, target: "str | InstanceHandle", method: str, payload: Value, *, parent: CallContext) -> Value: ...
    def stream(self, target: "str | InstanceHandle", method: str, payload: Value, *, parent: CallContext) -> AsyncIterator[Value]: ...
    async def start_call(self, target: "str | InstanceHandle", method: str, payload: Value, *, parent: CallContext) -> 'ExecutionHandle': ...
    async def spawn_instance(self, template: str, bindings: dict, config: dict, *, parent: CallContext) -> 'InstanceHandle': ...

async def apply(ctx: PluginContext, config: dict) -> None:
    async def run(payload: Value, call: CallContext) -> Value:
        return await ctx.call('model', 'infer', payload, parent=call)
    ctx.expose('run', run)
```

**语义约束**：

- `ctx.expose` 只导出当前实例的方法；`target` 可以是**局部绑定别名**，或由 `ctx.spawn_instance` / 授权宿主操作返回的 **InstanceHandle**。二者最终经同一授权调用路径，不存在隐藏的全局 Provider 选择。`ctx.self_handle` 是本实例受控 Handle，可用于本实例后台方法。
- `call/stream/start_call` 每次产生新的 Execution；`start_call` 返回可独立等待/取消的 Handle；`spawn_instance` 产生新 InstanceHandle 而不是隐式创建新 Agent，调用方可以用该 Handle 继续调用其导出方法。`on_ready` 收到内核生成的 Startup CallContext，可以通过 `ctx.self_handle` 登记后台任务，使其归属该实例 Scope，并在全局 READY 后启动；不得在 `on_ready` 内等待它完成。
- `CallContext` 的身份和 Scope 由 Kernel 及已认证 IPC 绑定，不能信任插件 payload 中自报的 `scope_id` 来提升权限。InstanceHandle 也是不可伪造、可撤销的 Host 句柄；跨进程用经过鉴权的 token 表示，不允许仅凭猜测 `instance_id` 调用任意实例。
- 子调用/子 Execution 默认由调用方当前 Execution Scope 所有，父调用结束时按结构化并发策略等待或取消未完成子活动。必须跨越父调用持续运行的任务，需由获授权的上级持久 Scope 持有；`on_ready` 登记的后台任务属于 Instance Scope。
- 同一实例允许 `max_inflight` 所限定的并发；默认 1（保守状态安全），可显式提高。超限需有**有界队列**或明确容量错误，等待可取消、受 Deadline 限制。对于一个调用栈内已经占满容量的目标实例，若等待同实例/循环回调会自锁，必须提前返回 `EXECUTION.REENTRANCY_DENIED` 或由实例显式允许可重入调用，不能无期限排队。Kernel 不理解业务并发含义。
- 公共远程方法须支持边界可序列化数据；`local_only` 仅限受信同进程，不能假装任意对象都可跨语言传输。
- 所有业务 Schema/返回值语义属于插件级合同；Kernel 检查可调用性、可序列化边界、授权和生命周期，不解析“这是不是模型请求”。

## 5. 跨进程/跨语言通信：v1 Wire Contract

- 可信同进程：直接异步调用；其安全等级是**受信代码**，不承诺强隔离。
- 进程隔离、Node.js/Rust、完整外部 Harness：`JSON-RPC 2.0 over stdio`，采用 **Content-Length 帧**（UTF-8 JSON；stdout 专供协议，诊断信息走 stderr）。不同传输将来可替换，但不能改变 `Execution`/`Scope` 语义。
- 通用 Worker 启动后与 Host 完成 ABI 版本和装载身份握手、注册实际公开导出；Host 才转发调用。Python 隔离 Worker 也使用同一协议，不需要为 Python 定制一套函数调用格式。
- 逻辑调用通过 `kernel.invoke` 及唯一请求 ID/Execution ID 关联；流通知采用 `kernel.progress`（序列号）；取消采用 `kernel.cancel`；正常最终结果通过同 ID 的终止响应返回。命名是 Kernel 传输协议，不是 Agent 专用接口。
- JSON-RPC 本身**没有**标准的流/取消/背压；必须实现有界待发送队列和接收端 credit/ack 机制，避免慢读者导致无限内存增长。超限时暂停生产者或以明确错误终止，不能悄悄丢失关键结果。
- 单个 RPC 请求的超时、取消与 Worker 退出都由 Supervisor 监督；取消先发送协作信号，Deadline 后可强制终止该 Worker/Scope。对于共享 Worker，尽量优先取消单次调用，不误杀无关请求；不能安全单次取消时，必须报告影响范围。
- `ResourceRef` 用于大型/二进制载荷；包含授权主体、有效期、内容身份、读取权限；不能以一个任意路径字符串绕过隔离文件系统。
- 连接断开且远程执行是否已经产生副作用不可确认时，返回 `EXECUTION.RESULT_UNKNOWN`，**绝不透明重试**。详细幂等性、业务重试和补偿由插件决定。

**示意消息**（字段为 Kernel 协议约束，不是现有实现）：

```json
{"jsonrpc":"2.0","id":"rpc-24","method":"kernel.invoke","params":{"execution_id":"ex-24","instance_id":"worker-a","export":"process","payload":{"text":"hello"}}}
```

该消息的源身份来自已认证的 Worker 通道，Kernel 不单凭消息中可伪造的字符串授权操作。跨进程 `stdout` 混入普通日志属于 `EXECUTION.PROTOCOL_ERROR`。

## 6. Supervisor：通用状态、报错、容错责任

### 6.1 Execution 状态与终止

`created -> running -> {succeeded, failed, cancelled, unknown}`；取消中的过渡状态为 `cancelling`。所有终态不可再变化。`unknown` 表示执行是否完成或是否有外部副作用无法确认，不是“普通失败”的同义词。

Supervisor 管理 Instance/Execution/Scope/Worker，不管理 Agent 的思考、步数、模型提供商、Plan 或业务恢复。进程树与容器由 Scope 归属；撤销 Handle、清理资源时不误杀仍被其他消费者使用的共享实例。

### 6.2 统一错误 Envelope

| 类别 | 例子 | 责任 |
|---|---|---|
| 装配 | `COMPOSITION.ENTRY_MISSING`、`COMPOSITION.INSTANCE_MISSING`、`COMPOSITION.EXPORT_MISSING`、`COMPOSITION.BINDING_MISSING`、`COMPOSITION.VERSION_MISMATCH` | Host 校验失败、拒绝启动 |
| 插件生命周期 | `PLUGIN.INIT_FAILED`、`PLUGIN.START_FAILED`、`PLUGIN.NOT_READY`、`PLUGIN.DISPOSED` | Host/Supervisor 回滚或拒绝调用 |
| 插件主动业务失败 | `PLUGIN.REPORTED_ERROR`，保留 `provider.rate_limited` 等原始码 | 插件或上层 Workflow 决定重试与恢复 |
| 未处理异常 | `PLUGIN.UNHANDLED_EXCEPTION`、`PLUGIN.WORKER_CRASHED` | Supervisor 分类与传播，清理影响资源 |
| 执行协议 | `EXECUTION.TIMEOUT`、`EXECUTION.CANCELLED`、`EXECUTION.TRANSPORT_LOST`、`EXECUTION.RESULT_UNKNOWN`、`EXECUTION.PROTOCOL_ERROR`、`EXECUTION.CAPACITY_EXCEEDED`、`EXECUTION.REENTRANCY_DENIED` | 传播确定/不确定结果，绝不自动重放业务 |
| 权限与系统资源 | `CONTROL.DENIED`、`CONTROL.RESOURCE_LIMIT`、`CONTROL.ENFORCEMENT_UNAVAILABLE` | Control 拒绝、隔离或终止 |
| Kernel 自身缺陷 | `KERNEL.INTERNAL_ERROR` | 明确作为内核 Bug，不混同插件错误 |

错误必须带：`code`、`message`、`plugin_id`、`instance_id`、`execution_id?`、`phase`、`cause?`（脱敏）、`outcome_known`（是否能确定外部效果）。资源清理的二次错误作为附加诊断，不能覆盖原始主错误。

- HTTP 429/超时/供应商返回错误由 Model 插件处理、重试或报告；Kernel 不懂 HTTP 429、不自动换模型。
- 插件主动返回 `{ok:false}` 是**调用成功完成但业务结果为失败**；插件以声明式 `PluginOperationError` 抛出为业务调用失败；其余未处理异常归为插件程序故障。
- 对跨进程未知副作用返回结果未知，重试/补偿由调用方做，不能假定不存在外部操作。
- 通用基础设施重启可以创建新 Worker，但**不得自动重执行上一条不确定的调用**。

## 7. Control：不可绕过的底层边界，业务预算不入内核

**强制约束对象**：Scope 中运行的进程/Worker、授权网络出口、文件系统挂载/路径访问、Secret 引用、CPU/内存/PID/Wall-clock/输出大小限制、取消和资源回收。

**不理解的业务限额**：LLM Token、上下文长度、模型请求次数、推理 Step、反思轮数、总美元成本、训练优化预算等，交由各自插件（可选 Budget/Gateway 插件）管理。

- 隔离 Worker 默认禁用直接公网出口；授权的代理/Gateway 可执行模型正文检查与计费，普通 HTTPS 隧道无法自动解密请求正文。
- `HTTP_PROXY` 环境变量不是强制隔离；对于任意 `requests/httpx/curl/socket` 代码，必须靠网络命名空间/防火墙/容器网络策略等 OS 权限边界实现强制出口控制。
- 可信同进程插件拥有宿主权限，不能宣称已对它实现强制网络/文件/执行时间隔离；需要硬约束的插件必须放到适当隔离 Worker。不可保障时拒绝严格 Profile，返回 `CONTROL.ENFORCEMENT_UNAVAILABLE`。
- 插件可以**申请**权限，但不能给自己授权。Scope 的有效权限不得超出父 Scope 授权；插件收到的凭据只能在相应授权边界中可见。不同插件共享进程时无法实现强凭据隔离。
- 对 API 费用严格上限的要求不是 Kernel 的 Token 预算规则，而是 Profile 明确依赖强制 Gateway 与 Budget 插件；若外部 Harness 允许直连，只报告真正可观察/约束的指标。

具体隔离后端可以更换，但 Control 的授权决定不能由受限插件自行绕过。隔离后台执行必须留有可审计的最小控制收据。

## 8. Event / Session / 状态与恢复

- Kernel 只记录 `instance/execution/scope` 身份、装载/取消/退出、授权拒绝和关键故障等**最小控制审计**；不定义 Agent 消息/工具观察/反思事件格式。
- 业务 EventBus、会话历史、上下文选择/压缩、Memory、完整 Agent Trace、实验指标全部通过插件实现。事件存储与模型可见的 Context 投影是独立策略。
- SessionStore 插件可以用纯内存或可靠持久存储；Kernel 不保证任意插件可断电恢复。业务插件自己定义可恢复状态、快照或事件重放。
- Kernel 恢复自身实例/运行身份时需要核验锁文件、已知 Scope 与 Worker 终态；不得把无法确认的在途外部操作当成未执行并重放。
- 研究 Profile 可强制加载不可变审计/实验记录插件，但这些专业记录格式不进入 Kernel ABI。

## 9. 版本锁定、更新与科研可复现

- 插件包锁：`plugin_id/version/digest/loader`；Composition 锁：实例配置（非秘密部分）、全部显式绑定、授权策略、允许动态模板、默认入口、必要运行约束。
- 运行身份还要记录实际调用入口与参数/任务标识、实验模型及业务配置的版本引用。`composition_digest` 不会因为每次调用不同而改变；每次 Invocation 独立 ID。
- 密钥只存引用（环境变量名/SecretRef），不在 Manifest、日志或 Artifact 中写明文。秘密实际值、外部 API 响应和现实环境变化不能仅凭锁文件保证确定性。
- Python/Node 的依赖锁与外部可执行文件/容器镜像身份应一并保存，避免“只锁插件版本但安装环境已变化”的伪复现。
- 安装/升级不替换活跃实例；新版本通过新 Composition 或开发态受监督 Drain/Restart 装配。科研 Run 禁止未记录的原地热替换。

## 10. 一个可运行系统如何成立？

**Kernel 的合法装配**：所有启用插件均完成装配与授权、实例处于 READY。可以没有入口，也没有 Agent。

**Kernel 的合法 Invocation**：存在唯一有效 `(instance_id, export_name)`；目标 READY、绑定完成、当前调用参数合法传输、受授权，然后启动 Execution。

**Agent 的业务成立**：完全由上层插件完成。例如 BasicLoop 需要模型/工具/上下文，则在 Manifest 中声明 `requires`；独立 Claude Code Backend 可以只提供自己的 `run` 导出。Kernel 不增设所谓 Agent 完整性判断，也不自动执行某个工作流。

典型实例：Hello Workflow、BasicLoop、共享 Model 的多 Agent、SkillEvo、外部 Claude Code/Codex Harness，都使用以上同一装载与执行机制。模型插件可封装多种协议适配器，由用户通过模型名、API URL、Key 环境变量引用及必要的协议选项进行配置；**Kernel 不解析不同模型响应、不要求每个模型写一个插件**，未知协议的支持由模型连接插件自行升级。真正外部 Harness 的内部模型/工具/上下文可自行管理，只对可观察的状态与控制能力负责，不伪称经过内核的模型请求或 Budget 插件。

## 11. 从当前 `SkillCompiler-PluginLab` 迁移的证据与要求

根据 2026-10-31 可行性报告：已有 Plugin/Registry/Host、依赖冲突检查、回滚、Scope 清理、两种模式、方法注册、Train→Val→Frozen→Test 兼容桥接；119 项测试通过（含 Docker Program 回归），新增真实模型 HTTP 为 0。**这些成绩只证明现有原型通过原有测试，不代表本规范的跨语言 IPC、安全隔离、并发 Supervisor、完整 Agent 生命周期已经实现。**

迁移策略：

1. 保留现有原仓库和 `src/skillcompiler` 的业务行为；继续在独立副本 `src/agentlab` 开发。
2. 从 FedAgentEvo 提取 Python 插件发现、接口校验思路，清除 federation-specific 字段；保留 Host/Scope 已完成的可复用能力。
3. **显式迁移差异**：原型拒绝依赖环；本 RFC 允许在两阶段装载下的纯引用环。需要用独立测试证明 `apply` 阶段不能依赖调用、Ready 阶段不会出现未校验导出。
4. Normal/Minimal 与 SkillCompiler 属于外部插件组合，不让其业务类型进入 Kernel。历史 Baseline 可保留自己的 Rollout/Worker，经独立方法插件接入；不能为统一而偷偷改变上游算法。
5. 保留原 Trace、费用与数据划分边界；完成固定轨迹行为对照后，才能称旧方法接入等价。训练、测试成绩不是 Kernel 架构测试。

## 12. 可验收的 Kernel v1 合同测试

| 编号 | 测试场景 | 必须满足 |
|---|---|---|
| K01 | 无入口装配服务组合 | `load` 成功；无入口 `run` 返回 `COMPOSITION.ENTRY_MISSING` |
| K02 | Composition 默认入口 / 启动时 `--entry` | 都能启动被指定导出；显式覆盖记录于 Invocation，不更改锁定 Composition |
| K03 | 两个导出/多个 Provider | 不推断入口，不自动挑选 Provider；每个绑定槽位唯一 |
| K04 | A→C，A.helper→B；替换 B2 | Kernel 和 A 无需修改，执行结果随绑定变化 |
| K05 | 纯引用环 A↔B | 两阶段装载成功；`apply` 中调用依赖明确失败；同步回调自锁被拒绝或明确许可重入，无永久死锁 |
| K06 | 插件声明与实际导出不符、缺依赖、版本错 | 在业务执行前失败并回滚，不残留可调用句柄 |
| K07 | 同包多实例、共享 B、不同 Scope | 实例状态隔离；取消一个消费者不销毁共享 B |
| K08 | `spawn_instance` 派生实例 | 限定于批准模板，Scope/权限正确继承；不能越权 |
| K09 | Python 进程内与 Node/Python stdio | 同一 ABI 调用得到等价控制/错误/身份结果 |
| K10 | 并发上限和有界排队 | 不出现无限堆积，超额有明确结果，等待可取消 |
| K11 | Streaming、取消、慢消费者 | 有序片段、终止响应、背压；单调用取消不误杀无关调用 |
| K12 | Worker 崩溃、半途 RPC 断线 | 分类准确；可能已发生副作用则 `RESULT_UNKNOWN`，无透明重试 |
| K13 | API 429、业务错误、未捕获异常 | 模型插件自己决定重试；Kernel 错误来源准确，不二次重试 |
| K14 | 清理故障/装载失败 | 主错误不被覆盖；受影响 Scope 资源回收，Handle 失效 |
| K15 | 网络/文件/密钥越权（requests/curl/socket） | 隔离执行被 OS 策略阻断；未获强制边界时拒绝声称严格隔离 |
| K16 | 超时、CPU/内存/PID 限制 | 可强制执行的 Worker 限额生效，Kernel 保留控制收据 |
| K17 | 组合/源码/依赖锁变化 | 旧 Run 无法伪装同身份恢复，新 Run 生成不同身份 |
| K18 | 不加载 BasicLoop/任何业务 Session | Kernel 仍完成纯计算插件的装载/执行/取消 |
| K19 | BasicLoop/共享模型多 Agent/外部 Harness 模拟程序 | 全程不改 Kernel，只通过插件、绑定和装配实现 |
| K20 | 现有 SkillCompiler + 至少一种真实外部 Baseline | 固定输入轨迹对照请求、Tool、评分、训练资产等行为，不误判算法提升 |

以上测试可先用本地假模型/本地 HTTP 服务与隔离 Worker 完成，不需要真实付费 API；K20 的真实环境接入另作回归，成本和实验效果与 Kernel 合格性分开评价。

## 13. 实施顺序与交付界限

- **Phase A — 接口/装配**：规范 Manifest、Composition、PluginContext、入口选择、二阶段装载、错误 Envelope；K01–K08。
- **Phase B — 执行/通信**：Execution/Scope/取消/限流、JSON-RPC Worker、流与结果未知；K09–K14。
- **Phase C — Control**：进程/容器隔离、网络文件密钥策略、硬资源限额与审计；K15–K17。
- **Phase D — 通用性**：纯计算、BasicLoop、共享多实例、模拟外部 Harness；K18–K19。
- **Phase E — 旧方法复用**：SkillCompiler 兼容桥、真实 Baseline 适配、固定轨迹等价；K20。

**交付定义**：本 RFC 是“可直接进入实现”的目标规范，不是代码交付完成证明。实现开始无需重新讨论 Kernel 是否认识 Agent、模型或 Workflow，也不能通过修改 Kernel 来迁就某个算法；发现本 RFC 内真实不可满足的合同，应附带反例、影响范围和最小 ABI 修订，而不是暗改运行语义。

---

## 14. 审核依据与和初版 RFC 的关系

- [初版 RFC](AgentKernel_v1_Architecture_RFC.md)：已有统一 ABI、三模块 Kernel、三类关系、显式绑定、通信/错误/安全/预算/版本及验收章节；本版修订了入口、跨语言 loader、装载阶段循环引用、RPC 帧与背压、执行身份、权限继承、测试范围等细节。
- [可行性报告](plugin-foundation-feasibility.md)：当前真实实现与已有代码的事实依据；报告中的测试数量不被本规范扩大解释。
- DSH/Cordis 的插件入口、注入与作用域设计属于参考；本规范的显示绑定、RPC Worker 和强制 Control 不声称是 DSH 的原生机制。
- JSON-RPC 2.0 是请求/响应协议，流/取消/背压是这里定义的额外 Kernel 传输合同，不应误认为标准自动提供。

## 附录 A. v1 Wire Protocol 的最小确定合同（规范性）

这部分用于避免“选择 JSON-RPC，但每位实现者各自发明握手与取消协议”。它不涉及任何 Agent 业务字段。

**帧**：`Content-Length: N\r\n\r\n` + 恰好 N 个 UTF-8 JSON 字节。每个消息是合法 JSON-RPC 2.0 请求、通知或响应；一个 Worker 的 stdout 只能写帧，stderr 承担文本日志。必须拒绝超出 Profile 上限的帧、错误长度、非法 JSON、重复的活动请求 ID。通信双方可并发处理多个不同 ID；同一 Execution 的进度序号必须严格递增。

**宿主 → Worker**：

| 方法 | 类型 | 作用 |
|---|---|---|
| `kernel.initialize` | request | 下发 `kernel_api / plugin_definition_digest / instance_id / config` 与受限启动凭据，Worker 调用插件 `apply` 并回报实际 `exports`；仅注册，不调用依赖 |
| `kernel.activate` | request | Host 完成所有实例的 Link/Export 校验后下发；Worker 运行有限 `on_ready`，回报完成（后台任务只登记，后续启动） |
| `kernel.invoke` | request | 传 `execution_id / export / payload / parent_id? / deadline?`，获得最终响应或统一 Error Envelope |
| `kernel.cancel` | request（双向） | 传 `execution_id`，允许 Host 或持有授权 Handle 的调用插件请求合作取消；收到接受状态不等于执行已停止 |
| `kernel.credit` | notification（双向） | 为 `execution_id` 增加 N 个进度发送额度（N 正整数）；任何发送方不可超额发送 |
| `kernel.shutdown` | request | 进入 Drain，等待或取消在途调用后清理并退出 |

**Worker → 宿主**：

| 方法 | 类型 | 作用 |
|---|---|---|
| `kernel.call_host` | request | 插件 `ctx.call/stream` 通过已绑定的局部 `alias` 或已授权的 `instance_handle` + `export` 调用目标实例；Host 校验通道身份、Handle、授权与 Scope 后派发 |
| `kernel.start_call` | request | 请求创建独立 Execution，并返回受控 Handle/Execution ID |
| `kernel.wait_execution` | request | 对自己持有的 ExecutionHandle 等待终态，并取回结果/错误；受 Deadline/取消约束 |
| `kernel.spawn_instance` | request | 请求用已授权模板创建子实例；Host 校验锁、权限继承与拥有者，返回可用于后续 `call_host` 的授权实例 Handle |
| `kernel.progress` | notification（双向） | `{execution_id, seq, value}`；Worker → Host，或 Host → 等待流的 Worker；只可在获得 credit 的额度内发送 |

对于跨 Worker 的 `ctx.stream`，Host 必须将实际被调用 Execution 的进度路由回等待方，使用相同的 `execution_id` / `seq` 与 credit 语义；普通 `call_host` 的最终 response 仍代表终止结果。Host 不应在每条 Stream 片段都重新做全量 Provider 解析，只通过预先绑定 Handle 转发。

**握手顺序**：建立受限进程（不继承未授权 Host Secret/环境变量）→ `initialize` 回报实际导出 → 所有实例 Link/Validate → `activate` 全部成功 → 全局 READY → 允许普通 `invoke`、启动已登记后台 Execution。`activate` 中失败导致装配回滚。无 ready 之前的依赖业务调用返回 `PLUGIN.NOT_READY`。`kernel.initialize` 和 `kernel.activate` 不能使用业务方法名自动替代。`kernel.wait_execution/cancel` 在 Worker → Host 方向必须校验当前进程确实持有对应的 ExecutionHandle。

**终止与断线**：单次 `invoke` 收到成功 response 才可标记 succeeded；规范错误 response 标记 failed/cancelled；请求已转发而远端连接断裂且副作用不可判定时标记 `RESULT_UNKNOWN`。`kernel.cancel` 的已接受应答**不等于**目标已停止；需等执行终态或强制回收。共享 Worker 被迫终止时，应将同进程其他未完成请求也标记为受影响，并分别判断结果是否未知。

**调用鉴权**：`instance_id/scope_id` 等消息字段用于关联，权限判断必须根据已认证 Worker 通道、绑定表和当前 Execution/Scope 完成；不能让外部插件以改写 JSON 中的 ID 获取别的插件权限。

**v1 的范围限制**：不强制支持任意对象跨语言 RPC，不支持“进程断线后透明恢复任意协程堆栈”，不实现自动业务重试；这些要求不能偷偷由 Kernel 转换成特定 Agent 规则。

## 附录 B. 历轮设计决策的覆盖检查（审计索引）

| 此前明确要求 | 最终规范位置 | 最终判定 |
|---|---|---|
| Kernel 薄内核、无 Agent/Workflow/Model 专用接口 | §0、§10 | 已覆盖 |
| PluginHost 统一装载，不再叠加 Service Ports 注册器 | §0、§2–3 | 已覆盖 |
| 所有插件遵守同一 Manifest/ABI；仅公共 `kernel_api` + 包版本 | §2.1 | 已覆盖 |
| 多 Provider 平等共存，但槽位显式唯一绑定 | §2.2、§4 | 已覆盖 |
| 依赖绑定图、归属树、运行时调用图分开 | §1 | 已覆盖 |
| 装载不需要入口；执行时通过任意实例导出函数启动 | §2.3 | 已覆盖，修正原 RFC |
| 组合只负责装配，A→C 流程由插件自己决定 | §2、§10 | 已覆盖 |
| 允许动态子实例、多 Agent、共享 Provider | §3.2、§4 | 已覆盖，并补充可调用 Handle |
| 同进程与跨语言 IPC 统一调用语义 | §4–5、附录 A | 已覆盖，含流/取消/背压协议 |
| Supervisor 通用监督，业务错误重试由插件处理 | §6 | 已覆盖 |
| Control 的硬隔离与 Token/对话预算分离 | §7 | 已覆盖 |
| `requests/curl/socket` 的强制出口限制与可信边界 | §7 | 已覆盖；不夸大同进程隔离 |
| Event、Session、Context、记忆与恢复策略插件化 | §8 | 已覆盖 |
| 源码/配置锁定、多实例、热更新与科研恢复 | §9 | 已覆盖 |
| Normal/Minimal、SkillEvo、外部 Harness 的不同执行路径 | §10–11 | 已覆盖 |
| 119 项已有测试 ≠ 新 Kernel 已完成 | §11–13 | 已覆盖 |
| 源原型拒绝依赖环与最终纯引用环的差异 | §3.1、§11、K05 | 已显式标明迁移项 |

**审核结论**：此前已确定的架构原则在本文中均有明确归属；实现时尚需将抽象合同翻译成具体代码、序列化结构和平台适配，但不需要为普通 Agent 业务再做新的 Kernel 架构决策。
