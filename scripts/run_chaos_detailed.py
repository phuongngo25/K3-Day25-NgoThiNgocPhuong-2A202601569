from __future__ import annotations

import argparse
import json

from reliability_lab.chaos import load_queries, run_scenario
from reliability_lab.config import ScenarioConfig, load_config
from reliability_lab.metrics import RunMetrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/metrics.json")
    parser.add_argument("--detail-out", default="reports/metrics_by_scenario.json")
    args = parser.parse_args()

    config = load_config(args.config)
    queries = load_queries()

    scenarios = config.scenarios or [ScenarioConfig(name="default", description="baseline run")]

    per_scenario: dict[str, object] = {}
    combined = RunMetrics()
    for scenario in scenarios:
        result = run_scenario(config, queries, scenario)

        if not scenario.provider_overrides:
            passed = result.availability >= 0.95
        elif len(scenario.provider_overrides) > 1:
            passed = result.availability >= 0.5
        else:
            passed = result.fallback_success_rate >= 0.9

        report = result.to_report_dict()
        report["pass"] = passed
        per_scenario[scenario.name] = report

        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    combined.write_json(args.out)
    combined.write_csv(args.out.replace(".json", ".csv"))
    with open(args.detail_out, "w") as f:
        json.dump(per_scenario, f, indent=2)
    print(f"wrote {args.out}, {args.out.replace('.json', '.csv')}, {args.detail_out}")


if __name__ == "__main__":
    main()
