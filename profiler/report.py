"""Render a Markdown decision report from a saved or new experiment."""

from html import escape


def _cell(value):
    return escape(str(value)).replace("|", "&#124;").replace("\n", " ").replace("\r", " ").replace("`", "&#96;")


def _money(amount):
    if amount is None:
        return "—"
    return f"${amount:.6f}"


def _percentage(numerator, denominator):
    return f"{numerator / denominator:.0%}"


def _gate(row):
    if not row["requirements_met"]:
        return "Failed"
    return "Passed" if row["cost_complete"] else "Passed, cost incomplete"


def render(data):
    settings = data["run"]
    variants = {row["id"]: row for row in data["variants"]}
    ranking = data["ranking"]
    baseline = variants[settings["baseline"]]
    rows = [
        "# AgentBench Lab comparison", "",
        f"Baseline **{_cell(baseline['id'])}** · {settings['repetitions']} attempts per configuration · "
        f"{settings['started_at_utc']} UTC", "",
    ]
    if settings["mode"] == "fixture":
        rows.extend((
            "> **Offline demonstration.** Model replies, token counts, and rates are fixtures. "
            "Sandbox checks and end-to-end timings were measured locally.", "",
        ))
    rows.extend((
        f"Requirements are a {_cell(settings['slo_s'])} s deadline, "
        f"{settings['min_pass_rate']:.0%} minimum acceptance, "
        f"{settings['min_on_time_rate']:.0%} minimum on-time acceptance, and at most "
        f"{settings['max_critical_failures']} failed critical checks.", "",
        "| Configuration | Accepted | Accepted on time | Critical failures | p95 latency | Known cost | Cost per accepted, on-time task | Requirements |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ))
    for row in variants.values():
        total = row["attempts"]
        rows.append(
            f"| {_cell(row['id'])} | {row['accepted']}/{total} | {row['on_time_accepted']}/{total} | "
            f"{row['critical_failures']} | {row['p95_ms']:.1f} ms | {_money(row['known_cost_usd'])}"
            f"{'' if row['cost_complete'] else ' (partial)'} | {_money(row['cost_per_on_time_accepted_usd'])} | {_gate(row)} |"
        )
    rows.append("")
    if ranking:
        best = variants[ranking[0]]
        rows.append(f"**Best measured configuration under these thresholds:** {_cell(best['id'])}.")
        if best["id"] != baseline["id"]:
            delta = best["p95_ms"] - baseline["p95_ms"]
            rows.append(f"Its p95 latency changed by {delta:+.1f} ms versus the baseline.")
    else:
        rows.append("No configuration met the requirements with a complete cost estimate.")
    rows.extend((
        "", "## Configuration and p95 trace", "",
        "| Configuration | Model | Workflow | Runtime / hardware | Concurrent tasks | Model calls / retries / repairs | p95 model / tools / visible tests / other | Trace ID |",
        "|---|---|---|---|---:|---:|---|---|",
    ))
    for row in variants.values():
        parts = row["p95_components_ms"]
        component_time = " / ".join(f"{parts[key]:.1f}" for key in ("model", "tools", "visible_tests", "other"))
        rows.append(
            f"| {_cell(row['id'])} | {_cell(row['model'])} | {_cell(row['workflow'])} | "
            f"{_cell(row['runtime'])} / {_cell(row['hardware'])} | {row['concurrency']} | "
            f"{row['model_calls']} / {row['transport_retries']} / {row['repairs']} | "
            f"{component_time} ms | `{row['p95_trace_id']}` |"
        )
    rows.extend((
        "", "The time breakdown comes from the actual attempt at the p95 rank. "
        "Latency covers model calls, tool waits, and any repair loop. Final scoring runs afterwards. "
        "Model spans are client-side HTTP timings in live runs.", "", "## Acceptance by case group", "",
        "| Configuration | Group | Checks passed | Failed check IDs |", "|---|---|---:|---|",
    ))
    for row in variants.values():
        for name, group in row["groups"].items():
            failures = ", ".join(_cell(check) for check in group["failed_checks"])
            rows.append(
                f"| {_cell(row['id'])} | {_cell(name)} | "
                f"{_percentage(group['passed'], group['total'])} ({group['passed']}/{group['total']}) | {failures or '—'} |"
            )
    rows.extend(("", "## Reading the results", "",
                 "- Cost includes all attempts, including failed ones. Missing priced usage makes the estimate partial and excludes that configuration from the cost ranking.",
                 "- An hourly estimate uses the observed serving window and configured utilization. Dedicated cost is charged once per configuration across concurrent attempts.",
                 "- Critical failures count scored critical mismatches. Build, transport, and execution failures reduce the acceptance rates and are also listed in the JSON.",
                 "- These are observations for this task set and run. Repeat with fresh tasks and representative traffic before making a deployment decision.", ""))
    return "\n".join(rows)
