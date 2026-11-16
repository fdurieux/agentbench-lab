# AgentBench Lab

Compare coding-agent workflows, model endpoints, and serving deployments on the same acceptance tests. The runner records whole-task latency and explains where time went in a trace. It ranks configurations that meet an acceptance floor and a deadline by estimated cost per accepted, on-time task.

## Run the offline demo

Use Linux, Python 3.11 or newer, and a sandbox-capable host with unprivileged user namespaces. The scorer uses Bubblewrap, `prlimit` from util-linux, and `libseccomp.so.2`. On Debian or Ubuntu, the packages are `bubblewrap`, `util-linux`, `libseccomp2`, and `python3`. The host also needs a system interpreter at `/usr/bin/python3`. The runner checks the sandbox before making any model calls.

```sh
python3 -m profiler run --config examples/demo.json --out out/demo
python3 -m profiler report out/demo/results.json
python3 -m unittest discover -s tests -v
```

The demo uses stubs for model replies, token counts, and rates. The runner executes the generated Python and measures local time. Two variants pass. The third passes visible checks and fails a hidden 14-day notice boundary. The example timings and costs illustrate the profiler rather than measuring a model or accelerator.

## Compare real endpoints

Copy [examples/live.json](examples/live.json) to `examples/live.local.json`. Replace the endpoint, model, runtime, hardware, and rates with your own settings. The adapter uses the non-streaming [Chat Completions API](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create). A provider must support the requested parameters and function calling when using the `tools` workflow.

```sh
python3 -m profiler run --config examples/live.local.json --out out/live
```

- **Compare workflows.** Keep the model, endpoint, and parameters fixed. Use `tools` for two allowlisted spec fragments or `inline` to supply both up front. Set `repair_attempts` to zero to omit visible testing inside the agent loop, or to a positive number to allow bounded repairs based on visible check IDs. Final acceptance is computed independently in both cases.
- **Compare models.** Keep the workflow and workload fixed, and set each variant's model ID. Put the name of an environment variable in `api_key_env` when the provider requires a bearer token. Leave it absent for a trusted local endpoint that does not require authentication.
- **Compare serving deployments.** Point variants at separately configured compatible endpoints. Hold the model revision and workflow fixed, record the runtime, quantization, and hardware in `runtime` and `hardware`, and compare at the same `concurrency`. Run separate experiments for cold and warm caches and record how you reset or warmed the server. The runner uses closed-loop concurrency and runs the final checks after the serving window. It measures full-client request time. For queue, prefill, decode, or accelerator attribution, collect [vLLM runtime metrics](https://docs.vllm.ai/en/latest/usage/metrics/) and benchmark serving workloads with [AIPerf](https://docs.nvidia.com/aiperf/getting-started/ai-perf-comprehensive-llm-benchmarking). This release compares deployments through the endpoint and does not collect hardware counters.
- **Set costs.** Use USD per million tokens for `input_per_million`, `cached_input_per_million`, and `output_per_million` on billed endpoints. An omitted cached rate inherits the input rate. For a dedicated endpoint give `hourly` for the whole deployment and `utilization` as the expected active share of its paid time. Dedicated cost is estimated as wall-clock serving window × hourly rate ÷ utilization, charged once regardless of concurrent requests. Either or both pricing components can apply. Provider tiering, separate cache-write charges, and external tools require additional cost inputs beyond this version.
- **Set thresholds.** Use `slo_s`, `min_pass_rate`, `min_on_time_rate`, and `max_critical_failures` for eligibility. Set `repetitions` to reflect the precision you need. The seeded variant order makes a run reproducible. Change the seed on repeated experiments to check for ordering effects. Use a fresh output directory for each run.

## Workload and results

The bundled [mortgage-servicing workload](workload/) is synthetic. It contains a task brief, a policy, an OpenAPI contract, and independent visible and holdout cases. To add a custom workload, copy that directory and pass `--workload <directory>`. Keep the four filenames and case schema. Each case supplies one JSON request, the expected response, a split, a reporting group, and a criticality flag. The candidate implements `evaluate(request)`, representing the policy function behind the API. Checks compare exact decoded JSON, including the response shape and semantics. Final scoring sends only the test inputs to isolated code and compares the responses with expected values in the parent process.

| File | Contents |
|---|---|
| `report.md` | Rankings, baseline comparison, observed p95 and its trace breakdown, and acceptance by case group |
| `results.json` | Per-attempt cost, timings, acceptance results, retry counts, and hashes for the config, workload, and runner |
| `traces.otlp.json` | OTLP JSON with agent, model, tool, repair, and scoring spans for an OTLP collector's HTTP traces receiver |

Delivery time starts when an attempt gets a worker and ends when the agent finishes or fails. It includes any visible checks and repairs. Independent final scoring is recorded separately. p95 is the nearest-rank observation over all attempts and can be noisy for small samples. An accepted result must pass every check, and on-time acceptance also requires delivery within the deadline. Failed attempts still count toward total cost. On priced endpoints, missing usage excludes a candidate from cost ranking. The known subtotal remains visible.

Reports and traces omit credentials, prompts, tool contents, and generated source. Keep private inputs and experiment manifests outside the public repository.

## Scope

This release runs one bounded Python code-generation task at a time. It supports fixture mode and OpenAI-compatible chat endpoints, with serial tools inside each agent attempt. It is useful for comparing measured deployment variants and diagnosing model waits, tool waits, and test overhead. Serving replay, streaming token timing, GPU counters, and general-purpose multi-agent tracing remain outside this release.
