from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from profiler.model import REFERENCE, Usage
from profiler.profile import _token_cost
from profiler.sandbox import Sandbox

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def stub_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.assert_path()
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            messages = body["messages"]
            last = messages[-1]
            usage = {"prompt_tokens": 1_000, "completion_tokens": 200,
                     "prompt_tokens_details": {"cached_tokens": 100}}
            message = {"role": "assistant"}
            if body["model"] == "missing-cost" and len(messages) == 2:
                usage = None
            if "tools" in body and len(messages) == 2:
                message["tool_calls"] = [{
                    "id": "read1", "type": "function",
                    "function": {"name": "read_spec", "arguments": '{"name":"policy"}'},
                }]
                message["content"] = None
            elif last["role"] == "tool":
                message["content"] = "def evaluate(request):\n    return {}"
            else:
                message["content"] = "```python\n" + REFERENCE + "```"
            response = json.dumps({"choices": [{"message": message}], "usage": usage,
                                   "model": body["model"] + "-snapshot"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def assert_path(self):
            if self.path != "/v1/chat/completions":
                raise AssertionError("incorrect endpoint")

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ProfilerTests(unittest.TestCase):
    def test_cached_rates_and_unavailable_usage(self):
        rates = {"input_per_million": 2, "cached_input_per_million": 0.5, "output_per_million": 8}
        known, complete = _token_cost([Usage(1000, 200, 100)], rates)
        self.assertAlmostEqual(known, 0.00345)
        self.assertTrue(complete)
        lower_bound, complete = _token_cost([Usage(1000, 200, None), None], rates)
        self.assertAlmostEqual(lower_bound, 0.00210)
        self.assertFalse(complete)

    def test_sandbox_bounds_execution(self):
        sandbox = Sandbox(1.5)
        sandbox.verify()
        checks, status = sandbox.evaluate(
            "def evaluate(request):\n    while True: pass", [{"id": "loop", "input": {}, "expected": {}}],
        )
        self.assertEqual((checks, status), ({"loop": False}, "judge_timeout"))

    def test_live_protocol_tools_repair_quality_cost_and_trace(self):
        with stub_server() as (url, requests), tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pricing = {"input_per_million": 2, "cached_input_per_million": 0.5,
                       "output_per_million": 8, "hourly": 3.6, "utilization": 0.5}
            config = {
                "mode": "live", "baseline": "tool-loop", "repetitions": 2, "slo_s": 5,
                "variants": [
                    {"id": "tool-loop", "model": "tool-model", "endpoint": url, "workflow": "tools",
                     "concurrency": 2, "repair_attempts": 1, "pricing": pricing},
                    {"id": "incomplete", "model": "missing-cost", "endpoint": url, "pricing": pricing},
                ],
            }
            manifest = folder / "config.json"
            manifest.write_text(json.dumps(config), encoding="utf-8")
            result = folder / "output"
            process = subprocess.run(
                [sys.executable, "-m", "profiler", "run", "--config", str(manifest), "--out", str(result)],
                cwd=ROOT, capture_output=True, text=True, timeout=24,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            rows = json.loads((result / "results.json").read_text(encoding="utf-8"))
            spans = json.loads((result / "traces.otlp.json").read_text(encoding="utf-8"))
            self.assertEqual(rows["ranking"], ["tool-loop"])
            summary = {row["id"]: row for row in rows["variants"]}
            baseline = summary["tool-loop"]
            self.assertEqual(baseline["accepted"], 2)
            self.assertEqual(baseline["repairs"], 2)
            self.assertEqual(baseline["model_calls"], 6)
            self.assertAlmostEqual(baseline["dedicated_cost_usd"], baseline["wall_seconds"] * 0.002)
            self.assertAlmostEqual(baseline["known_cost_usd"] - baseline["dedicated_cost_usd"], 6 * 0.00345)
            self.assertTrue(summary["incomplete"]["requirements_met"])
            self.assertFalse(summary["incomplete"]["cost_complete"])
            self.assertEqual(len(requests), 8)
            self.assertNotIn("holdout-", json.dumps(requests))
            spans = spans["resourceSpans"][0]["scopeSpans"][0]["spans"]
            self.assertTrue(any(span["name"] == "execute_tool read_spec" for span in spans))
            self.assertIn(baseline["p95_trace_id"], {span["traceId"] for span in spans})
            self.assertNotIn("NOTICE_TOO_SHORT", json.dumps(spans))
            self.assertIn("Best measured configuration under these thresholds:** tool-loop",
                          (result / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
