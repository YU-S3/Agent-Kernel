# AgentLab BasicAgent Reference Distribution & Kernel Integration Test RFC v1.0

> 状态：设计与测试合同定稿（Ready for implementation），不是已实现的测试结果。日期：2026-11-03。  
> 依据：`AgentKernel_v1_Architecture_RFC_FINAL.md`、`AgentLab_BasicLoop_Visual_Plugins_Design.md`、`plugin-foundation-feasibility.md`。  
> 工程约束：只在 `SkillCompiler-PluginLab` 独立副本实现；不修改生产 SkillCompiler/历史实验；新增默认离线且零真实模型请求。  
> 核心原则：Kernel 不引入 Tool、Agent、Subagent、Memory、Sandbox 业务特判。所有业务功能都是遵循同一 Plugin ABI 的普通插件。

## 0. 交付定义与验收目标

交付三个分离对象：

- **BasicLoop**：只有模型推理、工具调用、观察回传、继续/结束的简单 Agent Driver 插件；不实现注册中心、沙箱、子代理、存储或 GUI。
- **BasicAgent Reference Distribution**：以 Composition 显式装配的完整基础 Agent，覆盖模型、Tool Registry/Provider、History、Context、Workspace/Sandbox、基础策略、必要观测。可选扩展含子 Agent、MCP Adapter、Budget、持久存储、Approval。
- **Kernel & BasicAgent Conformance Suite**：系统验收集，验证模块可替换、组合不需要改 Kernel，且执行、作用域、资源、错误、回放可靠。不能把纯 Mock 小例子成功等同于产品级安全或完整 Agent 成功。

**架构通过标准**：不修改 `kernel/`，只修改插件包及 Composition，就能通过单 Agent、动态 Tool、隔离执行、多 Agent、持久记忆、不同 Driver 和外部完整 Harness 的测试。Visualizer 不修改基础 Renderer，也能自动呈现新节点、绑定、实际调用、父子 Scope；只有增加专业细节时才装扩展 Renderer。

## 1. 分层与插件职责

| 层 | 插件/组件（建议 ID） | 主要导出 | 负责的语义 | 强制依赖 |
|---|---|---|---|---|
| Kernel | `PluginHost/Supervisor/Control/ABI` | 通用装载/调用/Scope | 装配、执行、授权、通用故障 | 无 Agent/Tool 特判 |
| Driver | `agent.basic-loop` | `run` | 推理→工具→观察循环、停止与步骤预算 | model, tools, history, context |
| Model | `model.mock`, `model.universal` | `infer` / 可选 `stream` | 协议编解码、LLM provider 错误、调用收据 | 外部 HTTP 服务可选 |
| Catalog | `tools.registry` | `register`, `unregister`, `list`, `execute` | 模型可见工具注册、Schema、路由、作用域可见性 | provider 函数/可选 policy |
| Tool Provider | `tool.file`, `tool.shell`, `tool.python`, `tool.fixture-http` | `describe`, `execute` | 实际动作与工具结果 | Workspace / sandbox 按工具要求 |
| Tool Policy | `tools.policy.basic` | `authorize`, 可选 `request_approval` | 业务工具允许/拒绝/人工批准 | 授权策略 / 可选 approval UI |
| Sandbox | `sandbox.process` / `sandbox.container` | `run`, `cancel`, `inspect` | 受控代码执行、进程/文件/网络限制 | Kernel Control 的真实隔离执行能力 |
| Workspace | `workspace.fixture` | `open`, `read`, `write`, `list`, `resolve` | 工具可访问的数据与工作区 | 文件访问授权 |
| Facts | `history.memory`, `history.sqlite` | `open`, `append`, `read` | append-only 消息/工具观察存储 | 可选持久存储 |
| Context | `context.full`, `context.window` | `build` | 当前模型可见上下文投影 | history |
| Subagent | `subagent.manager` | `delegate`, `status`, `cancel`, 可选 `followup` | 父子 Agent 业务语义与结果整合 | 受授权的模板、Agent Driver |
| Subagent Tool | `tool.delegate-agent` | `describe`, `execute` | 把委派公开给模型，与其它 Tool 一样注册 | subagent.manager |
| Control-policy | `budget.demo` | `reserve`, `settle`, `remaining` | Token/调用/任务预算，非 Kernel 预算 | 需要时由 Composition 显式绑定 |
| Observability | `visual.bridge`, `visual.web`, `trace.store` | `snapshot`, `events` 等 | 只读通用执行可视化/事件转发与回放 | Kernel 通用观察出口 |
| Alternative Driver | `agent.single-step`, `agent.plan-execute` | 自选入口 | 不同执行控制流 | 按方法声明 |
| External | `harness.mock-jsonrpc` | 自选入口 | 模拟完整外部 Agent Driver | Kernel IPC/Supervisor |

**工具注册与“插件注册”不同**：Kernel PluginHost 只装配插件；ToolsRegistry 是业务插件，管理给模型看的工具目录。其 ToolDefinition/ToolCall/ToolResult 类型均属于 BasicAgent 生态合同，不进入 Kernel ABI。

**Tool Sandbox 与 Plugin Isolation 不同**：Tool Provider 使用 sandbox.execute 运行 Shell/Python；不受信插件本身仍需由 Kernel Control 放到隔离执行环境。只安装 `tools.policy` 不能取代 OS/容器权限控制。

## 2. Tool Registry 的可实现合同

### 2.1 注册结构（BasicAgent 业务协议）

```json
{
  "name": "read_file",
  "description": "Read one file in the authorized workspace",
  "input_schema": {"type":"object","properties":{"path":{"type":"string"}},"required":["path"],"additionalProperties":false},
  "output_schema": {"type":"object"},
  "binding": {"instance":"tool_file","export":"execute"},
  "side_effect": "read",
  "concurrency_safe": true,
  "timeout_ms": 5000
}
```

- **注册 API**：`register(registration, scope_ref)`，返回 `registration_id` / `RegistrationHandle`；`unregister(registration_id)` 撤销新调用入口；`list(caller_scope)` 只返回获准的模型可见 JSON Schema，不带 provider 内部信息、凭据与执行回调。
- **静态注册**：Composition 显式配置 Registry 的 Provider 绑定，Registry 在 Ready 后调用 `describe` 导入工具。**动态注册**：已绑定/获授权 Provider 可以在运行中调用 `register`，其登记资源跟随注册 Scope 的生命周期自动撤销；不可在 `apply` 时越过 ready barrier 调依赖。
- **重复名称**：同一调用可见目录内同名工具拒绝装载/注册（或由 Composition 显式设置模型可见别名），不允许加载顺序静默覆盖。不同 Scope 可有不同目录。
- **调用步骤**：模型输出 ToolCall（有唯一 `id` 和参数）→ Registry 查找 `name` → 检查调用 Scope 可见性 → 验证 JSON 参数 → Tool Policy/Approval → Kernel 授权执行边界 → Dispatch 到被绑定的 Provider → 校验/截断 ToolResult → 返回 Observation。
- **防绕过原则**：不能仅隐藏工具 Schema 却允许通过 `execute(name)` 任意调用；`list` 和 `execute` 使用同一权限/可见性解析。ToolRegistry 的业务拒绝不能被后续 Provider 覆盖；Kernel Control 的文件/网络/密钥限制独立强制。
- **运行中的注销**：不会取消已准入调用，除非注册所有者的 Scope 已撤销或明确取消关联 Execution；后续请求应拒绝。保留原始 `tool_call_id` 与 `execution_id` 关联。
- **并发规则**：v1 默认一条模型响应内的工具调用按输出顺序执行，确保副作用可预测；后续可选择 `concurrency_safe` 并行，但必须记录入队顺序、开始与完成顺序。Kernel 不解释工具并发标记，它们由 Registry/Loop 遵守。

### 2.2 ToolCall / ToolResult（BasicAgent 业务协议）

```json
{
  "tool_call": {"id":"call-001", "name":"read_file", "arguments":{"path":"data.txt"}},
  "tool_result": {"tool_call_id":"call-001", "status":"ok", "content":[{"type":"text","text":"alpha\nbeta\ngamma"}], "is_error":false}
}
```

- 模型适配器保留 Provider 原始响应收据，并把结构映射为统一 ToolCall；Schema 失败返回格式明确的工具观察/协议错误（根据 BasicLoop 约定），不能让半条 Assistant ToolCall 永久处于未结算状态。
- Tool 的业务失败（文件不存在、命令返回非零）一般作为 `is_error=true` 的 ToolResult 回给模型；未处理插件崩溃、IPC 断线、Kernel 超时则走 Supervisor 错误 Envelope，未确定副作用标记 `RESULT_UNKNOWN`。两者不能混淆。
- 一个模型回复可含文本和多个 ToolCall；先保存 Assistant Message 再匹配各 `tool_call_id` 的结果。最终回答只在模型明确停止、或 Agent 业务策略判定终止时输出。

## 3. Sandbox / Workspace / Approval

- `tool.shell` 和 `tool.python` 通过**已绑定**的 Sandbox 插件执行命令；不允许在主宿主进程偷偷执行动态代码。推荐首版 `sandbox.container` 使用固定 digest 的最小镜像、工作区只读输入与明确允许的输出区、默认禁止外网、CPU/内存/PID/时间/输出限制；具体 OS 限制由 Kernel Control/受信 Sandbox Backend 落地。
- `tool.file` 使用工作区根目录与资源授权，规范化路径并处理符号链接；防止 `../`、符号链接跨出授权区域。测试中使用 fixtures 下的文件，不访问真实用户家目录。
- `tools.policy.basic` 按 Tool Definition 的操作/Scope 允许、拒绝或请求 Approval。审批等待不是 Shell 可执行权限。审批拒绝不能启动真正的 Tool Execution。
- 对可产生文件/网络副作用的超时结果，不自动重试；如果执行结果可能未知，应让调用方看到 `outcome_known=false`，由业务插件决定下一步。
- **部署诚实性**：没有 Linux namespace/cgroup 或容器权限时，只能测试 mock Sandbox 的调用合同；不得声称已经完成真实安全隔离。已存在的 SkillCompiler Docker Program 回归并不能直接证明新 Sandbox/Control 的安全性。

## 4. 子 Agent：两种入口，同一底层实例机制

**逻辑委派与 Kernel 动态实例分开**。Kernel 只有 `spawn_instance/start_call/cancel/Scope`；`subagent.manager` 负责：选用模板、配置子任务、维护父子业务身份、分配可见工具、独立 History/Context、结果汇总。

- **Workflow delegation**：Parent Orchestrator `ctx.call('subagents','delegate', task)` 得到子执行句柄；可以并发等待、取消和收集多个结果。这里不需要模型显式选择委派。
- **Model-selected delegation**：普通 `tool.delegate-agent` 注册为一个 Tool，模型通过 ToolCall 触发委派，并最终收到对应 ToolResult。Registry 不因它是 Agent 而特殊处理。
- 子 Agent 默认独立会话，父子身份由 Manager 的业务记录关联；History Fork 仅在需要时由 Manager/History 插件实现。共享 Model Provider 不意味着共享 Session。
- 子 Agent 的权限是父 Scope 授权与子配置请求的交集，**不得扩大权限**；其取消与清理由通用 Scope/Execution 完成，不让兄弟 Agent 互相终止。共享 Model Provider 由上层公共 Scope 持有，不随单个子 Agent 退出而卸载。
- 防无限递归由 `subagent.manager` 的业务层委派深度/数量限制以及 Kernel Control 的系统资源上限共同约束。模型 Token/费用预算可选 `budget` 插件跨子 Agent 统一仲裁。
- 对外部 Harness 子 Agent，仅展示其 adapter 公开的执行进度和结果；如果 SDK 不支持 `followup`，不应声明支持可持续子会话。DSH 的 Subagent 系统也区分本地可继续会话与外部单次执行（见引用）。

## 5. History / Context / Budget / 模型

- `history.memory`/`history.sqlite` 拥有完整消息事实；`context.full/window` 决定每次模型请求看到什么，window 不能删除事实存储。
- 固定的 session_id + invocation identity 可跨多轮沿用；相同 session 的并发写入必须明确串行或持久提交顺序。取消后下一次运行不应复用未结算 ToolCall 造成配对错乱。
- `model.mock` 默认覆盖：最终回答、单工具调用、多工具调用、并行模型请求、流式部分消息、HTTP 429、协议畸形、超时和拒绝。`model.universal` 的真实 Provider 冒烟测试是独立的 opt-in 阶段（必须显式提供预算和 API Key）；不要把它混进零 HTTP 回归测试。
- Token/费用/对话轮数属于 `budget`、Model、Agent Driver 或 Experiment 插件；Kernel Control 只处理不可绕过的权限和系统硬资源限制。严格付费预算需要受控出口/记账 Provider；未实现时明确降级可观测性或拒绝该严格约束。

## 6. 动态 Visualizer：数据合同和交互要求

### 6.1 通用图数据，不按插件名字特殊判断

- **Topology**：来自 Composition 快照的 `instance_id/plugin_id/bindings/entry`；动态 `instance.spawn/instance.dispose` 会更新结构。
- **Execution**：来自 Kernel 的通用只读执行观察流（`started/progress/finished/failed/cancelled`、caller/target/method/execution_id），不猜测调用方内部实现。
- **Scope**：来自 Kernel 的资源归属事实，而不是从 A→B 调用关系推断。
- **Business details**：ToolCall 参数、Model token、History size、子 Agent 委派语义等来自业务插件授权的细节事件/Renderer；Generic Renderer 没有“遇到 model 就画成模型”之类特判。
- **UI 必须允许**：拖动背景平移、滚轮在指针位置缩放、± 缩放、适配全部节点、拖拽单个节点重新布局；图形不能因固定 SVG 大小而裁切。新节点出现时可手动 Fit 或启用动态 Fit；重绘事件不能擅自重置用户正在查看的位置。
- **观察不能改变 Agent**：UI 断开或慢消费不影响执行；脱敏输入/输出预览；节点布局只作为 UI 状态，不改变 Composition 或 Kernel 绑定。长运行回放由可选 TraceStore 提供，不能靠浏览器内存充当唯一日志。

### 6.2 关键 UI 场景

`viz-basic`, `viz-tool-sandbox`, `viz-subagent`, `viz-multi-agent`, `viz-memory`, `viz-dynamic-spawn`, `viz-external` 和 **随机插件名称**的泛化场景。UI 要保留三张不同关系图的语义：依赖图、实际调用图、归属图。

## 7. 完整离线测试矩阵（全部应在 0 新 HTTP 模型请求下运行）

优先级：**G0 = 交付阻断**；**G1 = 完整 BasicAgent 集成验收**；**G2 = 可选能力/真实 Provider 单独验证**。原则上 G0 + G1 全部通过才称为“完整 BasicAgent 集成测试通过”，不能用 UI 演示或 Mock 算法分数代替。

| ID | 等级 | 用例 / 触发 | 必须验证的结果 |
|---|---|---|---|
| AG01 | G0 | Kernel 无入口，只 `load` | 加载成功；执行时未指定入口报 `COMPOSITION.ENTRY_MISSING` |
| AG02 | G0 | BasicLoop Mock 两步工具任务 | `read_file` 执行一次，ToolCall 配对，第二次模型得到观察并结束 |
| AG03 | G0 | 工具动态注册/撤销 | 注册后 list/dispatch 可见；注销后不可见且新调用被拒绝，Provider Scope 清理 |
| AG04 | G0 | 重名、无效 Tool Schema、无效参数 | 失败被准确归因；不执行实际 Provider；同名不静默覆盖 |
| AG05 | G0 | 同一 Tool 的不同 Scope 可见性 | 子 Agent 不可见未授权工具；不能绕过 `list` 直接调用 `execute` |
| AG06 | G0 | 同一响应中的两个 ToolCall | ID 无混淆；结果按计划写入历史；文本与 ToolCall 共存 |
| AG07 | G0 | Tool 业务失败、Model 429、插件未捕获异常 | ToolResult、Provider 业务错误、Supervisor 故障按各自层归因；Kernel 不二次重试 |
| AG08 | G0 | Shell/Python 使用真实 Sandbox | 只执行一次，结果/退出码/标准输出受限并可追溯；不在宿主执行动态代码 |
| AG09 | G0 | Sandbox 文件逃逸、网络直连、超时、进程树 | 操作被禁止或终止，资源回收；不能仅 Mock 声称执行隔离 |
| AG10 | G0 | 取消 ToolCall 或断线 | `EXECUTION.CANCELLED/RESULT_UNKNOWN` 传播正确，副作用不透明重放 |
| AG11 | G0 | Tool 注册中途失败/插件装载失败 | Host 回滚、工具解绑、Worker/Scope 清理，原始错误不被遮蔽 |
| AG12 | G0 | 可视化任意命名的插件拓扑 | 节点/绑定/Entry 自动生成；未知插件无需通用前端改代码 |
| AG13 | G0 | 前端视口交互 | 拖背景、节点移动、缩放、Fit、动态 Spawn 后不裁切；用户手动视图不被每次事件重置 |
| AG14 | G0 | UI 关闭/慢监听 | Agent 结果与事件因果不变；有界传输或丢包标记，无执行阻塞 |
| AG15 | G1 | Parent 通过 ToolCall 委派子 Agent | 子 Session/Scope、返回 ToolResult、调用关联一致，父 Agent 继续推理 |
| AG16 | G1 | Parent 直接委派两个并发子 Agent | 子 Agent 独立 History，公共 Model 可共享，取消一个不影响另一个 |
| AG17 | G1 | 子 Agent 试图扩大工具/文件权限 | 子 Scope 无法扩大父授权；不可见工具无法执行 |
| AG18 | G1 | 子 Agent 崩溃/父 Agent 取消 | 正确故障传播、子执行清理、共享 Provider 保留 |
| AG19 | G1 | history.memory ↔ history.sqlite | 不改 BasicLoop；事实可读取，重启后 SQLite 恢复（memory 不承诺恢复） |
| AG20 | G1 | context.full ↔ context.window | 模型可见上下文不同，但原始 History 未被删改 |
| AG21 | G1 | basic-loop ↔ single-step/plan-execute | 仅改 Composition/业务插件；Kernel 和通用 UI 不修改 |
| AG22 | G1 | 动态 spawn_instance / 已授权 Handle 调用 | 新实例创建、调用、取消、回收；可视化自动出现且 Scope 正确 |
| AG23 | G1 | 多 Agent 同时使用同一个 Model Provider | 实例共享与并发上限正确，Session 无串扰，单 Agent 取消不卸载公共 Provider |
| AG24 | G1 | 外部 JSON-RPC 模拟完整 Harness | 入口、流式、取消、错误、退出按同一 Kernel ABI 表示；内部不可观察步骤不捏造 |
| AG25 | G1 | 模型流式响应及不完整 ToolCall | 未完 JSON 不执行；完整 ToolCall 后执行；取消/失败无悬空 ToolCall |
| AG26 | G1 | 注册 MCP fixture / adapter | 通过同一 ToolsRegistry 暴露，scope/Schema/错误与本地工具一致（无需外网） |
| AG27 | G1 | Trace live / replay | 关联的执行 ID、调用边、Scope 归属、最终状态一致；事件丢失明确告警 |
| AG28 | G1 | Budget 插件限制步数/Token（模拟） | 由插件拒绝/终止，Kernel 不理解 Token；共享父子预算不双重花费 |
| AG29 | G1 | 审批允许/拒绝工具调用 | 拒绝无副作用，批准才进入执行，审批事件与结果可追溯 |
| AG30 | G1 | 重复重跑固定 Seed 与冻结版本 | 事件相对因果、结果、工具次数/产物可对照；组合版本/摘要稳定 |
| AG31 | G2 | opt-in 真实模型冒烟 | 填 model/url/key/协议，完成 ToolCall 一轮，记录 usage，限制费用；需显式授权 |
| AG32 | G2 | 更复杂外部 CC/Codex 接入 | SDK/协议适配保留原生 Loop，只有已观察内容进入 UI |

### 对应 Kernel RFC 的回归

必须原样保留 Kernel RFC v1 的 PluginHost、Supervisor、Control 合同测试（依赖绑定、环、ready barrier、共享 Provider、IPC、结果未知、权限和组合身份）。本测试**新增 Agent 应用层验证**，不能将原内核测试覆盖掉或把旧 119 项测试当作新测试已通过。

## 8. 真实端到端固定任务（离线 Fixture）

**Task A — 读文件并总结**：工作区 `data.txt` 内容 3 行；模拟模型先输出 `read_file` ToolCall，再输出“3 行”；断言文件未改变、ToolResult `call_id` 唯一、模型第二请求包含观察。

**Task B — Shell/Sandbox**：模型请求 `shell` 对指定 fixture 运行 `wc -l data.txt`（或等价命令）；工作区 mount read-only；断言 stdout、exit_code、超时与日志；额外试图读取授权根以外文件，应被阻断。

**Task C — 子 Agent 委派**：父 Agent 要求两个子 Agent 分别核对文件摘要和测试结果，子 Agent 使用不同 History + 共享 Model；通过 `tool.delegate-agent` 接入 Registry；父 Agent 收到两个标识明确的结果，再给出最终答复。

**Task D — 动态工具**：同一运行中 Provider 注册名为 `inspect_metadata` 的工具，新模型步骤出现工具 Schema；注销后下一步骤不再出现，直接恶意调用也被拒绝。

**Task E — 更换 Driver**：同样任务输入，经 BasicLoop 和 PlanExecute 两种根插件执行，验证可运行、输出正确、静态图/执行图不同；并不要求二者模型请求次数相同。

**Task F — 外部模拟 Harness**：使用 Node/Python stdio JSON-RPC 进程，返回多条流式进度和最终文本；断线后上层看到 `RESULT_UNKNOWN`/明确 transport error，不自动重复执行。

## 9. 必需证据与自动化报告

每个测试生成隔离目录：`run_manifest.json`、`composition.lock`、`kernel_events.jsonl`、`business_trace.jsonl`（如有）、`tool_receipts.jsonl`、`result.json`、`cleanup_report.json`；安全测试额外输出实际隔离机制及阻断证据；前端 E2E 保留截图和测试结果。所有文件默认去除 API Key、真实用户 Prompt 或私人文件内容。

验收命令建议（**目标 CLI，非现有命令**）：

```bash
agentlab test --suite basicagent --offline --report-dir reports/basicagent
agentlab test --suite kernel-contracts --offline
agentlab visual serve --profile profiles/basicagent-viz.yaml
```

**发布门禁**：AG01–AG30 全部通过；原有 Kernel 合同测试通过；0 未授权 HTTP 模型请求；真实 Sandbox 权限测试有 OS 级证据；UI 拖拽/缩放/适配和动态实例可靠；且无 Kernel/通用 Renderer 为某个新 Agent 插件添加特殊分支。AG31/AG32 是独立增强验收，不阻塞离线 v1 基础架构交付。

## 10. 实施次序（按验证风险组织，不按插件数量堆砌）

1. **工具闭环与目录**：Mock Model + BasicLoop + History/Context + `tools.registry` + `tool.file`，先过 AG01–07。
2. **真实执行约束**：Workspace + Shell/Python + Sandbox/Control 端到端，完成 AG08–11；若隔离环境不可用，明确阻断安全验收而非偷换为 mock。
3. **通用可视化**：只读 Snapshot/Execution Feed，修复 pan/zoom/fit/节点拖动，完成 AG12–14。
4. **Agent 组合自由度**：Subagent + ToolDelegation + 同时多 Agent + History/Context 替换 + 替换 Driver + 运行时 Spawn，过 AG15–23。
5. **跨进程与更复杂场景**：外部模拟 Harness、流式、MCP fixture、Trace、Budget/Approval、冻结重复实验，过 AG24–30。
6. **可选在线验证**：授权后执行 AG31；再做真实 CC/Codex 完整 Harness 接入 AG32。

**避免修改 Kernel 的实质性判断**：如果新 Agent Feature 只能通过新增 `ctx.agent.*` 或 Kernel 特判才能工作，优先检查它是否可以实现为普通插件的导出函数、已授权 InstanceHandle 或业务层 Registry；确实缺少所有插件共有的底层执行语义时，再记录为 Kernel ABI 的一般性缺口，而不是添加某类 Agent 的专属规则。

## 11. 已有与未做

当前来自可行性研究的证据是 `agentlab` 原型已有基本插件 Host、模式和方法注册、部分 ToolRegistry/Scope 检查，119 项原型+原有测试通过；**这些不等于本文 AG01–AG30 已经实现或通过**。已有 `AgentLab_Runtime_Visualizer_Demo.html` 是离线 JSON 模拟；其可视化与真实 Kernel 之间的只读事件桥仍是后续开发工作。
