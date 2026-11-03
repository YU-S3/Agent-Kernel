# Basic plugin contracts

Basic is a runnable plugin distribution. Tool-loop, single-step, plan/execute,
memory, container, delegation, parallel, dynamic, approval, MCP and renamed
compositions share these contracts. Benchmark behavior remains in adapters;
the Kernel does not implement the Agent loop or scoring.

## Evidence and termination

`tool.searchqa` uses `visible-text-span.v1`: unseen body characters and title
text per authenticated consumer/task. Reading text already shown by search
does not create progress. Overlapping pages and duplicate bodies under
different IDs cannot create fictitious progress. URLs remain visible but do
not count as new evidence. Zero-overlap lexical queries return no results.

Three consecutive tool steps without new evidence trigger one tool-free final
request. This policy does not guarantee answer correctness. Historical facts
remain intact; context compaction changes only the request projection. Normal
answers, abstentions, truncation and filtering retain the actual finish reason,
request ID and response model. Filtering is not a successful answer.

## Tools, delegation and accounting

- Native and MCP file reads share workspace checks with explicit configured
  read permissions. Missing/non-UTF-8/unreadable files return controlled tool
  errors. Escapes and symlinks remain rejected.
- Python/shell observations include stdout, stderr, exit status, bounded
  artifacts and original output byte counts. Full sandbox receipts remain
  available. Docker absence is a controlled infrastructure failure.
- Child terminal status and reason survive delegation and workflow synthesis.
  Child observations are untrusted. Permissions, depth and per-episode child
  limits are enforced independently of model behavior.
- Preflight refusal before HTTP settles usage to zero and records
  `http_request_sent=false`. Request counters include admitted inference
  attempts; they do not prove a billable HTTP request occurred.
- A failed response with valid nonnegative integer `usage.total_tokens` is
  billed exactly, even if SSE ends without `[DONE]`. Protocol failure remains
  a failure and incomplete tool calls never execute. Partial response and raw
  usage stay in receipts. Missing/invalid usage remains unknown, never zero
  or estimated. Transport uncertainty is not automatically retried; only the
  existing bounded pre-HTTP transient DNS retry applies.

## SearchQA scoring versions

New selections freeze `evaluation` inside the hashed `selection.json`; the UI
displays its version. Old selections without this field keep their recorded
legacy scorer. `normalized-em-token-f1.v1` remains unchanged.

`searchqa.surface-alias.v2` is a **local** policy, not an official evaluator.
It normalizes Unicode/HTML typography, escaped apostrophes, word hyphens and
articles. Only explicit, non-nested trailing gold alternatives `A (or B)` and
narrow explanatory notes `A (for her/his/their role/performance/work ...)`
yield additional accepted answers. Imported `answers` lists are supported.
Predictions are not shortened to substrings or matched to invented synonyms.
Numeric signs, decimal points and `+` remain significant. Local and legacy
scores are both recorded. Gold/aliases never enter the agent input.

The [upstream repository](https://github.com/nyu-dl/dl4ir-searchQA) supplies data
and crawler code, not this evaluator. The
[author's dataset card](https://huggingface.co/datasets/kyunghyuncho/search_qa/blob/main/README.md)
describes a raw answer string. Scientific comparisons must freeze and share a
scorer across methods. Scoring-only replay is not a new agent experiment.

## Verification

`tests/test_basic_audit.py` uses the real Host, intercepted loopback HTTP and
pinned Docker to cover evidence overlap, scoring negative cases, selection
identity, actual MCP reads, file failures, child states, code observations and
known/unknown billing. `test_basic_hardening.py` covers context, finalization,
image capabilities and consumer/episode isolation. `test_basicagent.py` covers
lifecycle, permissions, approval, cancellation, cleanup, registry, history and
observers. `test_benchmark_live_modes.py` covers all live compositions. These
checks do not imply every external model supports every modality.
