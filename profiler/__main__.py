"""Run a coding-agent experiment or regenerate its report."""

import argparse
import json
from pathlib import Path
import sys

from .profile import load_config, load_workload, read_json, run
from .report import render
from .sandbox import SandboxError
from .trace import otlp


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m profiler")
    commands = parser.add_subparsers(dest="command", required=True)
    experiment = commands.add_parser("run", help="run and score benchmark variants")
    experiment.add_argument("--config", type=Path, required=True)
    experiment.add_argument("--workload", type=Path, default=Path("workload"))
    experiment.add_argument("--out", type=Path, required=True, help="new output directory")
    report = commands.add_parser("report", help="regenerate a Markdown report from results.json")
    report.add_argument("results", type=Path)
    try:
        args = parser.parse_args(argv)
        if args.command == "report":
            sys.stdout.write(render(read_json(args.results)))
            return 0
        if args.out.exists():
            raise ValueError("choose a new output directory")
        settings = load_config(args.config)
        workload = load_workload(args.workload)
        results, traces = run(settings, workload)
        args.out.mkdir(parents=True, exist_ok=False)
        write_json(args.out / "traces.otlp.json", otlp(traces))
        (args.out / "report.md").write_text(render(results), encoding="utf-8")
        write_json(args.out / "results.json", results)
        print(f"Report: {args.out / 'report.md'}")
        return 0
    except (KeyError, ValueError, OSError, SandboxError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
