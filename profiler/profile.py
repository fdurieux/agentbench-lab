"""Run and score matched coding-agent experiments."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, UTC
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import random
import re
import time
from urllib.parse import urlsplit

from .model import ChatEndpoint, Fixture, ModelError
from .sandbox import MAX_CODE, Sandbox, SandboxError
from .trace import Trace

SYSTEM = (
    "Implement the requested Python function. Return only the complete Python file as text "
    "or in one Python code fence. Use only the Python standard library. "
    "The spec fragments are authoritative. Your function takes a decoded JSON object."
)
SPECS = {"policy": "policy.md", "openapi": "openapi.yaml"}
RATES = ("input_per_million", "cached_input_per_million", "output_per_million", "hourly")
FIXTURES = {"fixture-reference", "fixture-short-notice"}


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _number(value, name, low, high, *, integer=False):
    if type(value) not in ({int} if integer else {int, float}) or not low <= value <= high:
        raise ValueError(f"{name} must be {'an integer' if integer else 'a number'} in [{low}, {high}]")
    return value


def _label(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", value):
        raise ValueError(f"{name} must use letters, numbers, dots, underscores, or hyphens")
    return value


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict) or not isinstance(config.get("variants"), list) or not config["variants"]:
        raise ValueError("config requires a nonempty variants list")
    defaults = {
        "mode": "fixture", "repetitions": 5, "seed": 42, "slo_s": 10, "request_timeout_s": 30,
        "judge_timeout_s": 4, "min_pass_rate": 1, "min_on_time_rate": 0.95,
        "max_critical_failures": 0,
    }
    settings = defaults | config
    for key, low, high in (
        ("slo_s", 0.001, 86400), ("request_timeout_s", 0.1, 3600),
        ("judge_timeout_s", 0.1, 60), ("min_pass_rate", 0, 1), ("min_on_time_rate", 0, 1),
    ):
        _number(settings[key], key, low, high)
    for key, high in (("repetitions", 10_000), ("seed", 2**32), ("max_critical_failures", 10_000_000)):
        _number(settings[key], key, 1 if key == "repetitions" else 0, high, integer=True)
    mode = settings["mode"]
    if mode not in ("fixture", "live"):
        raise ValueError("mode must be fixture or live")
    variants = []
    for entry in settings["variants"]:
        if not isinstance(entry, dict):
            raise ValueError("each variant must be an object")
        variant = {
            "workflow": "inline", "concurrency": 1, "repair_attempts": 0, "transport_retries": 0,
            "max_model_calls": 8, "parameters": {}, "hardware": "unspecified", "runtime": "unspecified",
        } | entry
        _label(variant["id"], "variant id")
        if not isinstance(variant["model"], str) or not variant["model"].strip():
            raise ValueError("variant model must be a nonempty string")
        if variant["workflow"] not in ("tools", "inline"):
            raise ValueError("workflow must be tools or inline")
        for key, low, high in (
            ("concurrency", 1, 128), ("repair_attempts", 0, 10),
            ("transport_retries", 0, 10), ("max_model_calls", 1, 60),
        ):
            _number(variant[key], key, low, high, integer=True)
        parameters = variant["parameters"]
        reserved = {"model", "messages", "stream", "n", "tools", "functions", "tool_choice", "function_call"}
        if not isinstance(parameters, dict) or reserved.intersection(parameters):
            raise ValueError("parameters must be an object without reserved chat fields")
        pricing = variant.get("pricing")
        if pricing is None:
            raise ValueError("each variant needs explicit pricing, including zero rates when appropriate")
        if not isinstance(pricing, dict) or set(pricing) - {*RATES, "utilization"}:
            raise ValueError("invalid pricing fields")
        pricing = {key: pricing.get(key, 0) for key in RATES} | {"utilization": pricing.get("utilization", 1)}
        if "cached_input_per_million" not in variant["pricing"]:
            pricing["cached_input_per_million"] = pricing["input_per_million"]
        for key in RATES:
            _number(pricing[key], key, 0, 1_000_000)
        _number(pricing["utilization"], "utilization", 0.0001, 1)
        variant["pricing"] = pricing
        if mode == "fixture":
            if variant["model"] not in FIXTURES:
                raise ValueError("unknown fixture model")
        else:
            endpoint = urlsplit(variant["endpoint"])
            if (endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username
                    or endpoint.password or endpoint.query or endpoint.fragment):
                raise ValueError("set a Chat Completions HTTP(S) URL without embedded credentials or query")
            if endpoint.scheme == "http" and endpoint.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError("HTTPS is required for non-loopback endpoints")
            if variant.get("api_key_env"):
                env_name = variant["api_key_env"]
                if not isinstance(env_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", env_name):
                    raise ValueError("api_key_env must be an environment variable name")
                if not os.environ.get(env_name):
                    raise ValueError(f"set the {env_name} environment variable before running")
        variants.append(variant)
    ids = [variant["id"] for variant in variants]
    if len(ids) != len(set(ids)) or settings.get("baseline") not in ids:
        raise ValueError("variant ids must be unique and baseline must name one variant")
    settings["variants"] = variants
    return settings


def load_workload(directory):
    directory = Path(directory)
    paths = {"brief": "README.md", **SPECS}
    parts = {name: (directory / filename).read_text(encoding="utf-8") for name, filename in paths.items()}
    cases_file = directory / "cases.json"
    cases = read_json(cases_file)
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases.json must contain a nonempty array")
    ids = set()
    for case in cases:
        case_id = _label(case["id"], "case id")
        if (case_id in ids or case["split"] not in ("visible", "holdout")
                or type(case["critical"]) is not bool or not isinstance(case["group"], str)
                or not isinstance(case["input"], dict) or not isinstance(case["expected"], dict)):
            raise ValueError(f"invalid or duplicate case {case_id}")
        ids.add(case_id)
    if {case["split"] for case in cases} != {"visible", "holdout"}:
        raise ValueError("the workload needs both visible and holdout cases")
    hashes = {name: sha256(text.encode()).hexdigest() for name, text in parts.items()}
    hashes["cases"] = sha256(cases_file.read_bytes()).hexdigest()
    return parts, cases, hashes


def _model_attributes(attributes, usage):
    if usage:
        attributes.update({
            "gen_ai.usage.input_tokens": usage.input_tokens,
            "gen_ai.usage.output_tokens": usage.output_tokens,
            "gen_ai.usage.cache_read.input_tokens": usage.cached_input_tokens,
        })


def _execute_tools(message, messages, fragments, trace, parent):
    calls = message["tool_calls"]
    if not isinstance(calls, list) or not 1 <= len(calls) <= 4:
        raise ModelError("invalid_tool_calls")
    packed = []
    answers = []
    for call in calls:
        try:
            identifier = call["id"]
            function = call["function"]
            arguments = function["arguments"]
            name = function["name"]
            if (not isinstance(identifier, str) or len(identifier) > 256 or not isinstance(arguments, str)
                    or len(arguments) > 1000 or name != "read_spec" or call["type"] != "function"):
                raise ValueError
            packed.append({"id": identifier, "type": "function", "function": function})
            args = json.loads(arguments)
            key = args["name"]
            if not isinstance(key, str) or key not in fragments or key == "brief" or set(args) != {"name"}:
                raise ValueError
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise ModelError("invalid_tool_calls") from exc
        with trace.span("execute_tool read_spec", "tools", parent, **{
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "read_spec",
            "profiler.spec_fragment": key,
        }):
            answers.append({"role": "tool", "tool_call_id": identifier, "content": fragments[key]})
    messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": packed})
    messages.extend(answers)


def _source(content):
    if not isinstance(content, str) or len(content.encode("utf-8", errors="replace")) > MAX_CODE:
        raise ModelError("missing_or_oversized_code")
    stripped = content.strip()
    fenced = re.fullmatch(r"```(?:python|py)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*", stripped, re.DOTALL | re.I)
    if fenced:
        stripped = fenced[1].strip()
    if not stripped:
        raise ModelError("missing_or_oversized_code")
    try:
        stripped.encode("utf-8")
    except UnicodeError as exc:
        raise ModelError("invalid_code_encoding") from exc
    return stripped


def _generate(client, variant, config, fragments, visible, sandbox, trace, parent, calls, transport):
    use_tools = variant["workflow"] == "tools"
    prompt = fragments["brief"]
    if not use_tools:
        prompt += "\n\nPolicy:\n" + fragments["policy"] + "\n\nOpenAPI:\n" + fragments["openapi"]
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    repairs = 0
    source = None
    for _ in range(variant["max_model_calls"]):
        with trace.span("chat " + variant["model"], "model", parent, **{
            "gen_ai.operation.name": "chat", "gen_ai.request.model": variant["model"],
        }) as span:
            attributes = span["attributes"]
            try:
                reply = client.complete(messages, use_tools, config["request_timeout_s"])
            except ModelError as exc:
                calls.append(exc.usage)
                _model_attributes(attributes, exc.usage)
                attributes["error.type"] = exc.code
                if exc.retryable and transport["retries"] < variant["transport_retries"]:
                    transport["retries"] += 1
                    continue
                raise
            calls.append(reply.usage)
            _model_attributes(attributes, reply.usage)
            attributes["gen_ai.response.model"] = reply.served_model
        message = reply.message
        if message.get("tool_calls"):
            if not use_tools:
                raise ModelError("unexpected_tool_calls")
            _execute_tools(message, messages, fragments, trace, parent)
            continue
        source = _source(message.get("content"))
        if variant["repair_attempts"] == 0:
            return source, repairs
        with trace.span("visible_tests", "visible_tests", parent) as check_span:
            results, status = sandbox.evaluate(source, visible)
            if status == "sandbox_error":
                raise SandboxError("the code sandbox stopped working during the run")
            check_span["attributes"]["profiler.checks_passed"] = sum(results.values())
        if all(results.values()) or repairs == variant["repair_attempts"]:
            return source, repairs
        if len(calls) == variant["max_model_calls"]:
            return source, repairs
        repairs += 1
        transport["repairs"] = repairs
        failures = ", ".join(case_id for case_id, passed in results.items() if not passed)
        messages.extend((
            {"role": "assistant", "content": message["content"]},
            {"role": "user", "content": f"Visible checks failed ({failures}). Recheck the rules and return the complete corrected file."},
        ))
    if source is not None:
        return source, repairs
    raise ModelError("model_call_limit")


def _token_cost(calls, pricing):
    known = 0.0
    complete = True
    rates = (
        pricing["input_per_million"], pricing["cached_input_per_million"], pricing["output_per_million"],
    )
    if not any(rates):
        return known, complete
    for usage in calls:
        if usage is None:
            complete = False
            continue
        cached = usage.cached_input_tokens
        if cached is None and rates[0] != rates[1]:
            complete = False
            known += (usage.input_tokens * min(rates[:2]) + usage.output_tokens * rates[2]) / 1_000_000
            continue
        cached = cached or 0
        known += ((usage.input_tokens - cached) * rates[0] + cached * rates[1]
                  + usage.output_tokens * rates[2]) / 1_000_000
    return known, complete


def _task(index, variant, config, fragments, visible, sandbox):
    trace = Trace()
    client = Fixture(variant["model"]) if config["mode"] == "fixture" else ChatEndpoint(variant)
    calls = []
    transport = {"retries": 0, "repairs": 0}
    status = None
    candidate = None
    tags = {"profiler.variant": variant["id"], "profiler.attempt": index}
    with trace.span("invoke_agent", "agent", **tags, **{"gen_ai.operation.name": "invoke_agent"}) as parent:
        try:
            candidate, _ = _generate(client, variant, config, fragments, visible, sandbox, trace, parent, calls, transport)
        except ModelError as exc:
            status = exc.code
            parent["attributes"]["error.type"] = status
    latency_ns = parent["duration_ns"]
    cost, cost_complete = _token_cost(calls, variant["pricing"])
    durations = trace.spans_by_category()
    components = {key: durations.get(key, 0) for key in ("model", "tools", "visible_tests")}
    components["other"] = max(0, latency_ns - sum(components.values()))
    outcome = {
        "attempt": index, "trace_id": trace.id, "latency_ms": latency_ns / 1e6,
        "components_ms": {key: nanos / 1e6 for key, nanos in components.items()},
        "status": status, "model_calls": len(calls),
        "transport_retries": transport["retries"], "repairs": transport["repairs"],
        "known_token_cost_usd": cost, "token_cost_complete": cost_complete,
        "reported_input_tokens": sum(item.input_tokens for item in calls if item),
        "reported_output_tokens": sum(item.output_tokens for item in calls if item),
        "calls_with_usage": sum(item is not None for item in calls),
    }
    return outcome, trace, candidate


def _score(run, cases, sandbox, config, variant_id):
    outcome, trace, candidate = run
    checks = {}
    status = outcome["status"]
    acceptance_ms = 0.0
    if candidate is not None:
        with trace.span("acceptance_tests", "acceptance_tests", trace.spans[0], **{
            "profiler.variant": variant_id, "profiler.attempt": outcome["attempt"],
        }) as span:
            checks, status = sandbox.evaluate(candidate, cases)
            if status in {"sandbox_error", "judge_timeout"}:
                if status == "sandbox_error":
                    raise SandboxError("the code sandbox stopped working during the run")
                span["attributes"]["error.type"] = status
            span["attributes"]["profiler.checks_passed"] = sum(checks.values())
            span["attributes"]["profiler.checks_total"] = len(cases)
        acceptance_ms = span["duration_ns"] / 1e6
    accepted = bool(checks) and all(checks.values())
    critical_failures = 0
    if status is None:
        critical_failures = sum(case["critical"] and not checks[case["id"]] for case in cases)
    outcome.update({
        "status": status or ("accepted" if accepted else "acceptance_failed"),
        "accepted": accepted, "on_time_accepted": accepted and outcome["latency_ms"] <= config["slo_s"] * 1000,
        "acceptance_ms": acceptance_ms, "checks": checks, "critical_failures": critical_failures,
    })
    return outcome, trace


def _summarize(variant, config, attempts, cases, elapsed_s):
    total = len(attempts)
    accepted = sum(row["accepted"] for row in attempts)
    on_time = sum(row["on_time_accepted"] for row in attempts)
    critical = sum(row["critical_failures"] for row in attempts)
    pricing = variant["pricing"]
    hardware_cost = elapsed_s * pricing["hourly"] / (3600 * pricing["utilization"])
    known_cost = sum(row["known_token_cost_usd"] for row in attempts) + hardware_cost
    cost_complete = all(row["token_cost_complete"] for row in attempts)
    eligible = (accepted / total >= config["min_pass_rate"] and on_time / total >= config["min_on_time_rate"]
                and critical <= config["max_critical_failures"] and on_time > 0)
    tail = sorted(attempts, key=lambda row: row["latency_ms"])[math.ceil(0.95 * total) - 1]
    groups = {}
    for case in cases:
        group = groups.setdefault(case["group"], {"passed": 0, "total": 0, "failed_checks": []})
        passed = sum(row["checks"].get(case["id"], False) for row in attempts)
        group["passed"] += passed
        group["total"] += total
        if passed < total:
            group["failed_checks"].append(case["id"])
    return {
        "id": variant["id"], "model": variant["model"], "workflow": variant["workflow"],
        "runtime": variant["runtime"], "hardware": variant["hardware"], "concurrency": variant["concurrency"],
        "attempts": total, "accepted": accepted, "on_time_accepted": on_time, "critical_failures": critical,
        "p95_ms": tail["latency_ms"], "p95_trace_id": tail["trace_id"], "p95_components_ms": tail["components_ms"],
        "wall_seconds": elapsed_s, "dedicated_cost_usd": hardware_cost, "known_cost_usd": known_cost,
        "cost_complete": cost_complete, "cost_per_on_time_accepted_usd": known_cost / on_time
        if on_time and cost_complete else None, "requirements_met": eligible, "groups": groups,
        "statuses": dict(Counter(row["status"] for row in attempts)),
        "model_calls": sum(row["model_calls"] for row in attempts),
        "transport_retries": sum(row["transport_retries"] for row in attempts),
        "repairs": sum(row["repairs"] for row in attempts),
    }


def run(config, workload):
    fragments, cases, hashes = workload
    sandbox = Sandbox(config["judge_timeout_s"])
    sandbox.verify()
    visible = [case for case in cases if case["split"] == "visible"]
    summaries = []
    attempts_by_variant = {}
    traces = []
    variants = config["variants"].copy()
    random.Random(config["seed"]).shuffle(variants)
    started = datetime.now(UTC).isoformat()
    for variant in variants:
        print(f"Running {variant['id']} ({config['repetitions']} attempts, concurrency {variant['concurrency']})", flush=True)
        with ThreadPoolExecutor(max_workers=variant["concurrency"]) as executor:
            before = time.perf_counter()
            runs = list(executor.map(
                lambda index: _task(index, variant, config, fragments, visible, sandbox),
                range(1, config["repetitions"] + 1),
            ))
            elapsed = time.perf_counter() - before
            scored = list(executor.map(lambda run: _score(run, cases, sandbox, config, variant["id"]), runs))
        outcomes = [outcome for outcome, _ in scored]
        traces.extend(trace for _, trace in scored)
        attempts_by_variant[variant["id"]] = outcomes
        summaries.append(_summarize(variant, config, outcomes, cases, elapsed))
    ranking = sorted(
        (row for row in summaries if row["requirements_met"] and row["cost_complete"]),
        key=lambda row: (row["cost_per_on_time_accepted_usd"], row["p95_ms"], row["id"]),
    )
    # Endpoint hosts and engine metadata can be meaningful in a private manifest.
    # Persist hashes and the public comparison fields, and avoid echoing request parameters.
    config_digest = sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    runner_hash = sha256()
    for source in sorted(Path(__file__).parent.glob("*.py")):
        runner_hash.update(source.name.encode() + b"\0" + source.read_bytes() + b"\0")
    metadata = {key: config[key] for key in (
        "mode", "repetitions", "seed", "slo_s", "min_pass_rate", "min_on_time_rate", "max_critical_failures", "baseline",
    )}
    metadata.update({
        "started_at_utc": started, "workload_sha256": hashes, "config_sha256": config_digest,
        "runner_sha256": runner_hash.hexdigest(), "execution_order": [variant["id"] for variant in variants],
    })
    return {"schema_version": 1, "run": metadata, "variants": summaries, "attempts": attempts_by_variant,
            "ranking": [row["id"] for row in ranking]}, traces
