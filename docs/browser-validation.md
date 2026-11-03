# Browser acceptance gate

Run `agentlab visual serve --profile profiles/basicagent-viz.yaml`, then use the actual browser UI. The evidence is a manual/integration gate, not a synthetic model-performance result.

Required path: localhost page → choose profile → Run → completed real result → inspect History, graph and receipts → disk replay.

Validate desktop and a mobile width, blank-page/console health, actual task completion, background pan, pointer zoom, Fit, node drag, camera preservation across live refresh, dynamic child creation/disposal, dependency/call/scope views, manual approval denial/allow, cancellation, and replay controls. Save screenshots under `reports/browser`.

Write `browser_validation.json` with exact frontend source digest (`agentlab.abi.digest` over a mapping of every recursive static relative path to the SHA256 of its bytes), viewport(s), run IDs, observation notes, screenshot paths, and boolean `checks`: run, pan, node_drag, zoom, fit, manual_camera_preserved, dynamic_spawn, replay, approval, mobile, console_clean. The AG13 test rejects missing or stale evidence. Inspect the evidence itself when reviewing a release; a hand-written boolean is not a substitute for its screenshots and actual observed transitions.

For answer presentation, verify an existing saved run → formatted answer → disk replay → readable history → original details. Check fenced code, tables, inline/display math and partial streaming. Use offline `viz-*` profiles for new UI test runs; never use a live profile merely to exercise a button. Renderer-only sample text is not a model result. The focused `tests/test_result_rendering.py` uses Playwright Chromium and accepts `AGENTLAB_CHROMIUM` for an existing browser executable.

The supplied v2 HTML is preserved under `docs/specs` for comparison. Its simulated preset feed was replaced by real localhost snapshot/events/replay. Its dark visual language and three graph semantics were retained. The sidebar now starts real fixture executions and shows actual results and approvals instead of editing a simulated event script.
