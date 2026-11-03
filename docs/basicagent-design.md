# BasicAgent integration: implementation decisions

This document resolves the supplied Kernel v1 and BasicAgent integration RFC into one executable distribution.

## Kernel / plugin boundary

The Kernel knows JSON-safe exports, instance handles, scopes, execution ancestry, permissions, deadlines, generic resource controls, and read-only observations. It does not know ToolCall, session, Agent, tokens, skill, benchmark, approval, or learning algorithms. Those concepts live in ordinary plugins. `visual.web` owns the loopback HTTP application; `visual.bridge` exposes read-only snapshots/events; `trace.store` replays the durable event stream.

The trusted local SDK adds `authority(call)` and `scope_cleanup(scope_ref, callback, parent=call)`. Authority is computed from the actual Supervisor ancestry and intersected grants, not payload identifiers. Scope callbacks may attach only to a live authenticated ancestor execution or instance scope. Local cleanup callbacks are in-process resource capabilities; remote workers use the established instance cleanup ABI. Remote workers can query authority and emit bounded observations through explicit IPC methods.

`python_dependencies` binds the real bytes of installed plugin implementation packages. Thin manifests cannot silently change their implementation while keeping an unchanged composition lock. These are generic module identities, not an Agent-specific Kernel import rule.

## Resolved design conflicts

1. **Delegation handle lifetime.** A Kernel parent cannot return and leave owned child executions alive. `subagent.manager.delegate` therefore stays active until its child terminates. A parent starts two calls with `ctx.start_call(...)`, receives two execution handles, and can wait/cancel independently. The manager creates fresh Driver instances and sessions with intersected tool permissions; common Model/History/Budget providers are held by the composition scope.
2. **Shared registry reentrancy.** Model-selected delegation holds an outer Registry execution while its child needs that same service. Default reentrancy remains denied, preserving v1 contracts. A service can explicitly opt into `execution.reentrant: true`; nested calls are admitted only if a capacity slot is immediately available, never queued behind an ancestor. The reference registry, delegation tool and manager opt in. No Agent-name special case was added to Supervisor.
3. **Static tool imports after READY.** Apply only registers exports. The reference distribution first completes Host load/READY, then transactionally invokes Registry bootstrap, which calls provider `describe`. If import fails, the batch rolls back and the distribution unloads the Host.
4. **Unknown effects.** Cancellation and transport loss remain Kernel envelopes with `outcome_known=false`. Registry receipts report unknown/cancelled; History settles all pending IDs. No tool effect is retried automatically. A later turn opens a clean scoped lease and sees the settled historical facts.
5. **Observer independence.** Kernel events enter a bounded JSON-safe ring (4,096 records; detail previews capped at 16 KiB) plus an optional durable JSONL audit. Consumers read by sequence; loss is explicit. The UI does not participate in model/tool flow. Approval observation is reconstructed from events without creating new execution work. Only an explicit approval decision changes execution.
6. **Trusted backend vs arbitrary source.** Python application plugins are trusted. Shell/Python tool source never runs in Host: the sandbox plugin uses a pinned Docker image, readonly input/root, no network, dropped capabilities, 128 MiB memory, 32 PIDs, 0.5 CPU, finite deadline and bounded output. Only `/output` is a writable exported artifact mount; symlinks are not exported.

## Application contracts

- Driver bindings: Model `infer`; Tools `list/execute`; History `open/append/read`; Context `build`; Budget `reserve/settle`.
- A complete assistant response is validated before any ToolCall executes. Text and multiple calls coexist. The response is persisted before effects; tools execute sequentially in response order. Partial streaming JSON never invokes a tool.
- Tool descriptions contain input/output schemas, side-effect flag, concurrency flag and timeout. Internal routing binds a registered provider alias/export, never a model-supplied instance name. Duplicate names fail; aliases must be explicit. Dynamic registration expires with its authorized Scope. The same visibility guard applies to list and execute. Admission survives unregister, but new admission fails. Non-concurrency-safe tools serialize; deadlines cancel their Kernel child calls.
- Policy and approval execute before provider dispatch. Denial produces an error ToolResult and no actual provider execution. Approval decisions are exposed only through the trusted local application boundary.
- File workspace resolution rejects absolute paths, traversal and every symlink component. The default workspace is readonly. No benchmark data is required.
- History memory and SQLite share the same append-only interface. Session leases serialize concurrent use; SQLite commits preserve order and reopen recovery settles orphaned calls. Window Context selects whole user turns and does not cut assistant/tool pairs or delete stored facts.
- Budget reservations are atomic and owned by caller Scope. Settlement is single-use and cannot exceed reservation. Unknown usage holds the full reservation; multiple children share one ledger. Token semantics never enter Kernel Control.
- Model mock emits deterministic fragments, final messages, tools, errors and refusal. Model universal is a disabled-by-default common chat transport, not a vendor-specific SDK. Online tests and accounting guarantees require separate explicit configuration.
- MCP fixture exchanges actual JSON-RPC method/result envelopes through a separate adapter, then uses the exact same Registry admission and dispatch path. It does not claim live Internet MCP interoperability.
- The external Node harness keeps its own exported behavior and uses the production framed ABI. The UI reports only its exposed execution/progress, not fabricated internal Agent steps.

## UI and persistence

The renderer receives only instance/entry/binding facts, execution facts and owner Scope IDs. The three graphs have different edges. Camera and manual node positions are independent of backend state. Live updates preserve manual camera state. Playback reuses event reducers and never invokes a plugin. Persistent replay remains available after server restart via `/api/replay/<run-id>`; the current-service list contains current-service runs only.

The development UI is loopback-only and rejects unexpected Origin/Host mutation. It is not a remotely multi-user authenticated service. New non-fixture deployments must supply their own authentication and approval boundary.

## Source independence

The old SkillCompiler application package, method bridges, benchmark assets, synchronous compatibility facade and legacy algorithm tests were archived outside this project. Only generic v1 Kernel implementation and contract fixtures were retained. All 43 existing Kernel contract tests remain unchanged. New BasicAgent tests are counted separately.

## WSL working-directory recovery

The real container regressions exposed an invalidated working-directory handle after Docker/DrvFS activity: later relative Path.resolve() calls raised FileNotFoundError. Application workspace, report and profile roots are now captured as absolute paths before execution; container CLI children start in /. The regression runs a real sandbox, then initializes another complete Agent, including approval/cancellation, in the same process.
