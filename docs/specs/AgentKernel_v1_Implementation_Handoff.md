# Agent Kernel v1.0 — 实现交付单（供 Codex / 开发者执行）

> 本单与 [最终 RFC](AgentKernel_v1_Architecture_RFC_FINAL.md) 配套。**RFC 是行为权威**；本单负责安排实现、测试和验收。当前并未声称已对本地 `D:/T3S_exp/SkillCompiler-PluginLab` 写入任何代码。

## 1. 核心开发任务

在独立副本 `SkillCompiler-PluginLab` 的 `src/agentlab` 上实现一个**与 Agent 业务无关**的插件执行内核。只允许存在三个逻辑模块：PluginHost、Supervisor、Control，三者共享统一 Plugin ABI。模型、工具、Agent Loop、Workflow、Session、SkillCompiler 都不是 Kernel 自有业务逻辑。

最终效果：一个普通插件可公开任意方法；Composition 显式指定包/版本/实例/依赖；`kernel.load()` 能无任务入口装载服务；`kernel.invoke(instance, export, payload)` 可以执行任何已授权公开方法；`kernel.run()` 在有默认入口时快捷调用；PluginHost 不理解 Agent。受信本地调用与隔离进程调用在同一逻辑 API 下运行。

## 2. 开始前必须核验的代码事实

1. 核对本地分支、HEAD、工作区修改；确认是独立 PluginLab，不对原 `SkillCompiler` 仓库改写。
2. 阅读已存在 `src/agentlab` 注册器、Host、Scope、ToolRegistry、Normal/Minimal、Pipeline 兼容逻辑，识别已实现的部分。不要复制第二套 Registry / ServiceContainer。
3. 阅读 `plugin-foundation-feasibility.md` 及本次最终 RFC，记录当前原型与规范的差异。尤其是**原型拒绝依赖环 → v1 允许纯引用环，但禁止 `apply` 期间调用其他插件**。
4. 锁定原有 119 项测试及其环境，不删除、不跳过真实 Docker Program 用例来宣称回归通过。

## 3. 分阶段实施（每阶段必须可独立测试）

### Phase A — 统一装配和入口（先做）

实现：`PluginDefinition / PluginInstance / Composition / Handle / Scope` 的正式结构；单一 `loader` 元数据；Manifest/Composition 解析与锁定；`requires/exports` 合同检查；`apply → link → on_ready → READY` 两阶段装配；唯一绑定；失败全事务回滚；根实例、共享实例与多实例；无入口 `load` 与明确入口 `invoke/run`。引入全量配置/导出预检，保证出错时不调用业务入口。

必须通过 K01–K08。**注意**：不得为了工作流运行引入 `AgentManager`、`WorkflowManager`、`ModelPort` 等业务内核模块。

### Phase B — Supervisor 和 IPC

实现 `Execution`、调用链/Scope、可取消 Handle、超时/状态/错误包；有界并发队列；通用进程 Worker；JSON-RPC 2.0 + Content-Length 帧；`initialize/activate/invoke/call_host/start_call/wait_execution/spawn_instance/progress/credit/cancel/shutdown`；stdio 多路复用；慢消费者背压；进程崩溃的未知结果与资源回收。

必须通过 K09–K14。使用本地 Python Worker 和 Node.js Worker 做跨语言回归，不使用真实模型。

### Phase C — Control

实现最小受信/隔离运行等级、Scope 资源归属、权限授予/收缩、Worker 的强制超时、内存/PID/CPU 约束、网络出口限制、文件/Secret 访问隔离与最小控制收据。对不能强制实现的设置明确返回 `CONTROL.ENFORCEMENT_UNAVAILABLE`。

必须通过 K15–K17。测试用 `requests`、`curl`、原生 Socket、本地模拟 HTTP 服务；不能只通过 Monkeypatch 成功就视为强制隔离通过。

### Phase D — 通用性演示

在 `plugins/examples` 或同等目录交付四个**纯插件应用**：

- Hello Workflow：组合 A→C，A 从显式 `helper` 槽位调用 B（无模型/Agent）。
- BasicLoop（最后才实现）：任务输入→模型推理→工具执行→观察→后续推理，使用假模型；循环不是 Kernel。
- Shared Provider：两个独立 Agent 实例共享模型实例，可独立取消和关闭。
- External Harness Mock：Node.js 子进程以统一 ABI 提供 `run`、流、取消；必要时再替换为真正 Claude Code/Codex SDK 的薄适配插件。

必须通过 K18–K19。整个过程中不增加 Agent 专用内核分支。

### Phase E — 真实研究算法迁移

将现有 SkillCompiler 实现作为原生方法/执行插件，保持 `src/skillcompiler` 的算法边界；至少迁移一种具有上游独立 Rollout/Worker 的真实 Baseline（不能假装都走 BasicLoop）。完成固定轨迹行为等价对比：消息、模型调用、工具调用配对、评分/成功来源、学习资产与费用记录。训练/测试数据隔离沿用旧流程，不把论文实验语义硬编码进 Kernel。

必须通过 K20；之后才可进行在线小规模模型验收。**Kernel 合格性不等于 SkillEvo 方法效果提升**。

## 4. Kernel v1 必须对外提供的最小公共操作

```python
# 逻辑接口示意；以最终 RFC 的安全和生命周期条款为准
host.load(composition)                  # 无默认入口也合法
host.invoke(instance_id, method, input) # 明确入口，创建 Execution
host.run(composition, input)            # 仅在有默认 entry 时合法
host.unload(composition_id)             # Drain + 释放拥有的资源

ctx.expose(name, async_callable)
ctx.self_handle                         # 当前实例受控 Handle
ctx.on_ready(callback)
ctx.on_cleanup(callback)
await ctx.call(alias_or_handle, method, payload, parent=call_ctx)
ctx.stream(alias_or_handle, method, payload, parent=call_ctx)
await ctx.start_call(alias_or_handle, method, payload, parent=call_ctx)
await ctx.spawn_instance(template, bindings, config, parent=call_ctx)

execution.wait()
execution.cancel()
```

对外可有 Python 包装糖衣，但跨语言语义不得分叉。`ctx.self_handle` 表示当前实例的授权引用；`spawn_instance` 返回的 Handle 也可以直接用作调用目标。`on_ready` 仅能登记在全局 READY 之后才调度的后台 Execution。

## 5. 必须避免的回归/设计退化

| 错误实现 | 为什么不允许 |
|---|---|
| 只要有一个 `run()` 方法就自动当入口 | 多个插件均可有 `run()`，无确定性、违背显式装配 |
| 要求 Profile 必须加载某个 `Workflow` 类型 | Kernel 被迫认识 Agent 应用语义 |
| 给 model、tool、session 各写一套 Kernel 装载 API | 破坏统一插件地位，后续每类新插件都要修改 Kernel |
| 启用多个 Provider 后按加载顺序自动选一个 | 配置不确定，实验不可复现 |
| 装配期依赖实例未注册就调用依赖方法 | 会出现装载顺序问题；必须两阶段注册/绑定 |
| 把 A→C 的执行顺序解析为 Kernel 的工作流 DSL | Kernel 退化为特定 Agent Framework |
| Kernel 自动重试 API 429 或断线后的工具调用 | 业务策略外溢，可能重复产生副作用或费用 |
| 通过 Python monkeypatch 宣称任意进程网络已隔离 | 插件可直接使用不同 HTTP 库/Socket 绕过 |
| 将 Token、规划轮数、实验金额设成 Kernel 固定计数 | 阻碍非文本和非标准 Agent；由上层 Budget Plugin 定义 |
| 让业务 Event/Session 进入 Kernel 状态模型 | 未来不同 Agent 模式需要持续修改 Kernel |
| 只锁 Plugin 版本，不保存代码和依赖实际身份 | 不能验证科研运行的真实同一性 |

## 6. 最终交付产物

- 可安装/可导入的 `agentlab.kernel` 代码：单一 ABI + 三类逻辑职责。
- Manifest/Composition 校验器、示例配置与生成后的不可变锁定清单。
- 本地 Python + Node.js stdio Worker 与对应的 RPC/SDK 薄层。
- Supervisor 的 Execution/Scope/Handle 状态、错误 Envelope、取消与清理测试。
- Control 的强制权限测试和平台限制说明，不能伪报安全能力。
- K01–K20 的自动化回归报告（早期 Phase 可先只覆盖对应编号）。
- 四个应用示例及至少一种真实外部 Baseline 的行为等价性报告。
- 原仓库未修改的证据、独立副本 Commit、测试命令、环境镜像/依赖锁、零付费模型 HTTP 执行记录。

## 7. 可直接交给编码 Agent 的执行指令

> 以 `AgentKernel_v1_Architecture_RFC_FINAL.md` 为规范性合同，以本交付单作为阶段与验收要求。从当前 `SkillCompiler-PluginLab` 工作树先核验已有实现和测试，再按 Phase A→B→C→D→E 逐阶段实现；任何阶段都不能为了跑通某个 Agent 而在 Kernel 添加模型、工具、Workflow、Session、Token 等特定业务逻辑。优先复用已有插件 Host、Scope、FedAgentEvo 的可迁移 Protocol，保持原 SkillCompiler 和现有 Baseline 执行语义不变。每完成一个 Phase 执行对应 Kxx 测试与现有回归，报告具体提交、变更文件、测试结果、未实现合同和下一阶段阻塞项。未经验证不得声称 Kernel 已完整支持隔离、崩溃恢复或真实 Agent 方法效果；实验过程中不发起付费模型 HTTP 调用，除非得到另外明确授权。
