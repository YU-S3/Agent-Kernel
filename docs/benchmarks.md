# Benchmark adapters and real Basic configurations

The Kernel has no benchmark logic. `benchmark.runner` calls a bound adapter (`open`, `score`, `close`), a bound Agent (`run`) and accounting (`remaining`). The dataset-specific plugin owns public projection, native evidence tools and private scoring. The same Host records all calls and the existing graph draws actual dependencies and execution events.

## Use SearchQA in the workbench

1. Save the model connection once. Every **Basic** configuration uses it; the `离线示例` entries remain deterministic contract tests.
2. Select **Basic · 工具循环** and **任务来源 → SearchQA**.
3. Choose a split, starting question (one-based) and number of questions. These values select a contiguous range in the dataset's recorded order. Nothing reshuffles or silently replaces the original split.
4. Click **开始评测**. Watch completed/total, current question, EM/F1, token usage, request count, errors and unknown usage. Each row's **轨迹** opens its separate conversation; the lower inspector and event log retain individual calls.
5. Use **本题结束后暂停** for a checkpoint. Closing the browser does not stop execution. **继续剩余题目** restores the frozen model/options and selection and skips completed/error rows. An API interruption is not a wrong answer; it remains a separate error, with unknown usage if appropriate. To deliberately rerun it, start a new experiment selecting that range.

Plan/execute, persistent memory, model-selected delegation and concurrent/dynamic delegation can also run SearchQA. Plan/execute makes a real tool-free planning request before executing. Child agents get the same current question's retrieval tools, separate histories and a shared usage ledger. A single-step profile may stop after a tool call without an answer, so it is not exposed as a complete benchmark solver. Sandbox/approval/dynamic-tool demos are interactive configurations rather than SearchQA evaluation configurations.

The local MCP configuration bridges real workspace file reads through MCP method envelopes. It is not an external MCP HTTP client. The external Node harness fixture remains a protocol test, not an implemented commercial SDK connection.

## Import data

The UI accepts UTF-8 JSON arrays, `tasks`/`data` lists, or JSONL. A row contains `id` (optional), `question`, `answer`/`answers` and `snippets`/`search_results`. Both original SearchQA search-result objects and Hugging Face's column-oriented representation are supported. Normalization v2 retains the question, public category, public metadata, snippet text/title/URL/related links and explicit image blocks. Category is sent with the question; available images remain structured input. Media references without provided media are explicitly identified. Gold and other private fields are excluded. Null upstream snippets remain as empty text with their original public title/URL; they do not remove a question.

```json
{"id":"q1","question":"Example question","answer":"reference answer","snippets":[{"title":"Source","text":"Public evidence","url":"https://example.org"}]}
```

Upload files up to 100 MiB from the UI, or import an upstream ZIP with its independently obtained SHA256:

```bash
agentlab dataset import --file /path/to/val.zip --split validation \
  --sha256 d219bad6ba199e9a2d244b2c7ea5b79638d05f4c0ae786e6c53bd3ee282d238c \
  --source-url https://huggingface.co/datasets/kyunghyuncho/search_qa/resolve/06907e45883b7cae435453b65d598447039fde79/data/train_test_val/val.zip
```

ZIP import streams JSON members into indexed JSONL without extracting archive paths. A run materializes only its selected rows. Upstream data and local runs are not distributed with the source repository. Source: [SearchQA authors](https://github.com/nyu-dl/dl4ir-searchQA), [author dataset repository](https://huggingface.co/datasets/kyunghyuncho/search_qa).

`runs/datasets/<digest>/manifest.json` records the original archive hash, split, normalization, order, count and any malformed-member rejection. Per-run `benchmark/selection.json` records selected IDs and identity; `public.json` and `gold.json` are separated. The Agent has no file tool that could access either the catalog or gold. Retrieval is deterministic lexical matching over the supplied current-question snippets, not a new Internet search. `normalized-em-token-f1.v1` lowercases, removes punctuation/articles and normalizes whitespace; EM matches any gold alias, and F1 is the best token-overlap F1. This scorer is versioned explicitly; it does not claim identical scores to every historical preprocessing/evaluator.

Progress reports `scored`, `errors`, `correct`, and both denominators: EM/F1 over scored episodes and `em_all_completed` over all settled episodes including errors. A settled batch containing execution/API errors is `completed_with_errors`; this status is also derived for legacy display without rewriting old records. Explicit abstentions and controlled context/request limits score their empty answer normally. `requests` counts the accounting plugin's model reservations, including failed attempts. Exact returned tokens and unknown usage remain separate. Native tools and subagent calls are visible in event receipts, not folded into model-request counts. No learning algorithm is enabled merely by selecting a split labelled Train: this interface runs the selected Agent against questions. A future learning method must be composed as its own plugin.

## Add another benchmark

Register an adapter and its tools as normal PluginDefinitions. Bind the adapter's public tools through `tools.registry`, its lifecycle/scorer to `benchmark.runner`, and the runner to the chosen Agent. Never expose gold through a model-accessible tool or prompt. Add its catalog loader and UI metadata in the application layer; no Kernel scheduler or graph renderer changes are required. Freeze data/selection identity and add tests for native tools, scoring, cancellation, independent episodes and accounting before enabling it.

## Evidence and source identity

Search uses stable inverse-document-frequency-weighted lexical overlap within the current task only. Repeated snippets retain IDs and titles but omit duplicate text; `read_snippet` reopens any snippet in 2,000-character pages. New evidence is tracked per authenticated consumer, so sibling agents do not hide evidence from one another. Repeated queries and pages do not count as progress.

The catalog ID includes the raw file SHA256 and normalization version (`searchqa.public-input.v2`). Reimporting a raw file creates a distinct projection identity; older datasets, selections, traces, scores and usage remain intact. This is an adapter version change, not a retroactive correction of historical accuracy.
