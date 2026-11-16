"""Minimal OTLP/JSON export for agent, model, tool, and evaluator spans."""

from contextlib import contextmanager
import secrets
import time


class Trace:
    def __init__(self):
        self.id = secrets.token_hex(16)
        self.spans = []

    @contextmanager
    def span(self, name, kind, parent=None, **attributes):
        span = {
            "name": name, "category": kind, "traceId": self.id,
            "spanId": secrets.token_hex(8), "startTimeUnixNano": str(time.time_ns()),
            "attributes": attributes,
        }
        if parent:
            span["parentSpanId"] = parent["spanId"]
        self.spans.append(span)
        started = time.perf_counter_ns()
        try:
            yield span
        finally:
            span["duration_ns"] = time.perf_counter_ns() - started
            span["endTimeUnixNano"] = str(int(span["startTimeUnixNano"]) + span["duration_ns"])

    def spans_by_category(self):
        durations = {}
        for span in self.spans:
            name = span["category"]
            durations[name] = durations.get(name, 0) + span["duration_ns"]
        return durations


def _attr(key, value):
    if isinstance(value, bool):
        typed = {"boolValue": value}
    elif isinstance(value, int):
        typed = {"intValue": str(value)}
    elif isinstance(value, float):
        typed = {"doubleValue": value}
    else:
        typed = {"stringValue": str(value)}
    return {"key": key, "value": typed}


def otlp(traces):
    spans = []
    for trace in traces:
        for span in trace.spans:
            properties = {key: span[key] for key in (
                "name", "traceId", "spanId", "startTimeUnixNano", "endTimeUnixNano",
            )}
            if "parentSpanId" in span:
                properties["parentSpanId"] = span["parentSpanId"]
            kind = 3 if span["category"] == "model" else 1
            attributes = [_attr("profiler.category", span["category"])]
            attributes.extend(_attr(key, value) for key, value in span["attributes"].items() if value is not None)
            spans.append(properties | {"kind": kind, "attributes": attributes})
    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", "llm-agent-profiler")]},
        "scopeSpans": [{"scope": {"name": "llm-agent-profiler", "version": "0.1.0"}, "spans": spans}],
    }]}
