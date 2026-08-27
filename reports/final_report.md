# Day 25 Reliability Report

Author: Ngô Thị Ngọc Phương (2A202601569)
Environment: Python 3.11.9 venv, Docker Desktop 28.1.1 (Redis 7-alpine via `docker compose up -d`)
Test run: `pytest -q` → **35 passed, 7 xpassed, 0 failed** (full log: [`reports/test_log.txt`](test_log.txt))

> **Note on `make report`:** the Makefile's `report` target runs `scripts/generate_report.py`, a bare-bones scaffold that only dumps `metrics.json` into a stub file — it is not the graded deliverable and running it again will overwrite this file with that stub. This document is the hand-authored report required by the "Phase 6: Report" instructions in the README (copy `report_template.md` → `final_report.md`, fill every section). Do not re-run `make report` after this file — regenerate metrics with `make run-chaos` or `scripts/run_chaos_detailed.py` instead, which do not touch this file.

## 1. Architecture summary

`ReliabilityGateway.complete()` implements a three-stage pipeline: semantic cache → circuit-breaker-guarded provider chain → static degraded fallback. Every provider call is wrapped by its own `CircuitBreaker`, so one provider's failures never block traffic to the next.

```
                         User Request (prompt)
                                 |
                                 v
                       +-------------------+
                       |  Gateway.complete |
                       +-------------------+
                                 |
                                 v
                    +------------------------+
                    |  Cache.get(prompt)     |
                    |  (n-gram cosine sim.,  |
                    |   privacy + false-hit  |
                    |   guardrails)          |
                    +------------------------+
                       HIT  |         | MISS
                            v         v
                 route=cache_hit:X  Provider loop (in config order)
                 return cached text   |
                                      v
                     +----------------------------------+
                     | CircuitBreaker["primary"].call()  |
                     |  CLOSED/HALF_OPEN -> try primary  |
                     |  OPEN -> fail fast (skip)         |
                     +----------------------------------+
                        success |        | ProviderError / CircuitOpenError
                                v        v
                    route=primary   +----------------------------------+
                    cache.set(...)  | CircuitBreaker["backup"].call()   |
                    return          |  CLOSED/HALF_OPEN -> try backup   |
                                    |  OPEN -> fail fast (skip)         |
                                    +----------------------------------+
                                       success |        | fails too
                                               v        v
                                   route=fallback   route=static_fallback
                                   cache.set(...)   text="service degraded"
                                   return           error=last_error
                                                     return
```

`ResponseCache` (in-memory) and `SharedRedisCache` (Redis-backed) implement the same `get(query) -> (value|None, score)` / `set(query, value, metadata)` contract, so the gateway is agnostic to the cache backend (`configs/*.yaml` `cache.backend: memory|redis`).

## 2. Configuration

Values from [`configs/default.yaml`](../configs/default.yaml):

| Setting | Value | Reason |
|---|---:|---|
| `failure_threshold` | 3 | Primary provider fails ~25% of calls at baseline, so 3 *consecutive* failures is rare under normal noise (~1.6% chance) but is reached almost immediately once a scenario pushes a provider to 50–100% failure — trips fast on real outages without flapping on ordinary noise. |
| `reset_timeout_seconds` | 2 | Short enough that a 100-request scenario (each request ~200–300 ms of simulated latency) gets several open→probe cycles within its run, so recovery behavior is actually observable in a short local chaos test. Production would use tens of seconds tied to the real provider's typical recovery time. |
| `success_threshold` | 1 | A single successful probe re-closes the circuit — appropriate for this fast-moving simulation. A stricter production breaker might require 2–3 consecutive probe successes (the implementation supports this — see `test_success_threshold_greater_than_one`). |
| cache `ttl_seconds` | 300 | Sample queries (`data/sample_queries.jsonl`) are FAQ/policy-style content that doesn't change minute-to-minute. 5 minutes balances staleness risk against hit-rate and keeps any accidentally-cached content from lingering. |
| `similarity_threshold` | 0.92 | Tried 0.85 first: near-duplicate but *semantically different* queries scored above it — e.g. "refund policy for 2024" vs "...2026" scores **0.915** under this n-gram+word cosine (measured directly, see §8), so 0.85 would have served a stale year's answer. Raised to 0.92 and paired with the explicit date/number false-hit guardrail (`_looks_like_false_hit`) so genuine paraphrases still hit while near-miss lexical collisions are caught by the guardrail rather than relying on threshold tuning alone. |
| `load_test.requests` | 100 per scenario (400 total across the 4 scenarios below) | Enough samples per scenario for a stable P50/P95 read without making a local run slow (~25–30s per scenario at ~250 ms average simulated latency). |

## 3. SLO definitions

Evaluated against the canonical deliverable `reports/metrics.json` (`make run-chaos`, memory-backend cache), which blends **all 4 scenarios including the intentionally extreme `both_degraded_70`** stress test:

| SLI | SLO target | Actual (blended, 4 scenarios) | Met? | Actual (steady-state, 3 realistic scenarios*) | Met? |
|---|---|---:|---|---:|---|
| Availability | ≥ 99% | 0.7900 | ❌ | 0.99 (avg of 0.99/0.99/0.99) | ✅ |
| Latency P95 | < 2500 ms | 319.64 ms | ✅ | 316–321 ms | ✅ |
| Fallback success rate | ≥ 95% | 0.5200 | ❌ | ~0.96 (avg of 0.972/0.976/0.933) | ✅ |
| Cache hit rate | ≥ 10% | 0.4825 | ✅ | 0.587 | ✅ |
| Recovery time | < 5000 ms | 2320.39 ms | ✅ | 2236–2404 ms (2 of 3 scenarios closed at least once; see §8) | ✅ |

\* *Steady-state = `primary_timeout_100` + `primary_flaky_50` + `all_healthy`, i.e. excluding `both_degraded_70`, which was deliberately configured to fail both providers 70% simultaneously as a worst-case stress test, not a realistic production condition — see §7/§8 for why blending it in drags the aggregate below target on purpose.*

## 4. Metrics

Full contents of [`reports/metrics.json`](metrics.json) (blended, memory-backend cache, 400 requests across 4 scenarios):

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 0.7900 |
| error_rate | 0.2100 |
| latency_p50_ms | 280.32 |
| latency_p95_ms | 319.64 |
| latency_p99_ms | 324.00 |
| fallback_success_rate | 0.5200 |
| cache_hit_rate | 0.4825 |
| circuit_open_count | 12 |
| recovery_time_ms | 2320.39 |
| estimated_cost | 0.049302 |
| estimated_cost_saved | 0.193 |

CSV export (`RunMetrics.write_csv()`) is at [`reports/metrics.csv`](metrics.csv), with each scenario flattened to a `scenario_<name>` column, e.g. `scenario_both_degraded_70=fail`.

Per-scenario breakdown (from [`reports/metrics_by_scenario.json`](metrics_by_scenario.json), 100 requests each):

| Scenario | availability | error_rate | fallback_success_rate | cache_hit_rate | circuit_open_count | recovery_time_ms | pass |
|---|---:|---:|---:|---:|---:|---:|---|
| primary_timeout_100 | 0.99 | 0.01 | 0.9722 | 0.64 | 5 | null | ✅ |
| primary_flaky_50 | 0.99 | 0.01 | 0.9762 | 0.47 | 4 | 2404.34 | ✅ |
| all_healthy | 0.99 | 0.01 | 0.9333 | 0.65 | 1 | 2236.45 | ✅ |
| both_degraded_70 | 0.19 | 0.81 | 0.0122 | 0.17 | 2 | null | ❌ |

## 5. Cache comparison

Two identical chaos runs (same 4 scenarios, 400 requests each), one with `cache.enabled: true` (`configs/default.yaml`), one with `cache.enabled: false` (`configs/no_cache.yaml` → [`reports/metrics_no_cache.json`](metrics_no_cache.json)):

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 276.05 | 280.32 | +4.27 ms (+1.5%) |
| latency_p95_ms | 318.10 | 319.64 | +1.54 ms (+0.5%) |
| estimated_cost | 0.133772 | 0.049302 | −0.084470 (**−63.2%**) |
| cache_hit_rate | 0 | 0.4825 | +0.4825 |
| availability | 0.7425 | 0.7900 | +0.0475 (+4.75 pp) |

Note on the latency delta: `run_scenario()` only appends a request's latency to `latencies_ms` when `latency_ms > 0` (per the `chaos.py` spec), and a cache hit is recorded with `latency_ms=0`. So the P50/P95/P99 columns above are computed only over the *actual provider calls* in each run — they aren't directly diluted by cache hits, and with no fixed RNG seed the two runs' provider-call latency samples aren't identical, which is why the delta here is small and even slightly positive (noise, not a real regression). The unambiguous, reproducible cache win is **cost** (−63.2% in this run, −58.9% in an earlier trial — consistently the dominant effect) and a measurable availability gain (a cache hit never touches a possibly-open circuit breaker, so it can't fail).

## 6. Redis shared cache

- **Why in-memory cache is insufficient for multi-instance deployments:** `ResponseCache._entries` is a plain Python list living in one process's memory. If the gateway is horizontally scaled (e.g. behind a load balancer, N pods), each instance builds its own cache from scratch — the same query pays for a fresh LLM call on every instance it happens to land on, hit rate is capped at roughly `1/N` of what a shared cache would achieve, and the false-hit/privacy audit logs are also process-local and can't be centrally inspected.
- **How `SharedRedisCache` solves this:** it stores each entry as a Redis Hash (`{prefix}{md5(query)}` → `{"query": ..., "response": ...}`) with `EXPIRE` handling TTL eviction automatically, so any gateway instance pointed at the same `redis_url`/`prefix` sees the same cache state immediately after a write, with no coordination logic needed in the application.

### Evidence of shared state

`pytest tests/test_redis_cache.py::test_shared_state_across_instances -v` — two independent `SharedRedisCache` instances (`c1`, `c2`) on the same Redis, same prefix; `c1.set(...)` then `c2.get(...)` returns the value `c1` wrote. All 6 Redis tests pass with Redis running (`docker compose up -d`):

```
tests/test_redis_cache.py::test_redis_connection PASSED
tests/test_redis_cache.py::test_set_and_exact_get PASSED
tests/test_redis_cache.py::test_ttl_expiry PASSED
tests/test_redis_cache.py::test_shared_state_across_instances PASSED
tests/test_redis_cache.py::test_privacy_query_not_cached PASSED
tests/test_redis_cache.py::test_false_hit_different_years PASSED
============================== 6 passed in 1.62s ===============================
```

### Redis CLI output

After running the chaos suite against `configs/redis_cache.yaml` (`cache.backend: redis`):

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:095946136fea
rl:cache:0bc3b1acf73d
rl:cache:3dab98c0e49e
rl:cache:734852f3cf4a
rl:cache:844ef0143a5c
rl:cache:8baa2cfa11fa
rl:cache:98332d0d1c9c
rl:cache:9e413fd814eb
rl:cache:b2a52f7dc795
rl:cache:d354658dc020
rl:cache:da61fb49b4f6
rl:cache:dacb2b833659
rl:cache:fff10da1c72c
(13 keys — one per distinct non-privacy-sensitive query seen)

$ docker compose exec redis redis-cli HGETALL rl:cache:844ef0143a5c
query
List three benefits of response caching in LLM gateways.
response
[backup] reliable answer for: List three benefits of response caching in LLM gateways.

$ docker compose exec redis redis-cli TTL rl:cache:844ef0143a5c
(integer) 265   # < 300s configured ttl_seconds -> confirms EXPIRE was applied on write
```

### In-memory vs Redis latency comparison

Same 4 scenarios, 400 requests, memory vs Redis backend ([`reports/metrics.json`](metrics.json) vs [`reports/metrics_redis.json`](metrics_redis.json)):

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms | 280.32 | 283.32 | Redis adds real network round-trips (`HGET`/`SCAN`) per lookup vs. an in-process list scan; still dominated by the ~200–300 ms simulated provider latency, so the backend choice barely moves the number. |
| latency_p95_ms | 319.64 | 315.96 | |
| cache_hit_rate | 0.4825 | 0.7275 | Higher on Redis mainly because this run's random query draws happened to repeat more (stochastic, no fixed seed — see §8), not purely a backend effect. |
| estimated_cost_saved | 0.193 | 0.291 | Tracks the higher hit rate above. |

## 7. Chaos scenarios

Scenarios defined in [`configs/default.yaml`](../configs/default.yaml) (the first three were provided; `both_degraded_70` is a scenario I added to exercise the worst case — both providers degraded simultaneously — which the original 3 scenarios don't cover).

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| `primary_timeout_100` | Primary always fails → breaker opens after 3 failures and, since primary can never succeed even on probe, should keep cycling OPEN→HALF_OPEN→OPEN (never fully closes) while all traffic is masked by backup fallback. | availability 0.99, fallback_success_rate 0.9722, circuit_open_count 5, recovery_time_ms **null** — matches exactly: the circuit opened 5 times and never closed, because every HALF_OPEN probe hits the deterministically-failing primary and immediately re-opens with reason `probe_failure`. Users still got a 99% success rate via `backup`. | ✅ pass (fallback_success_rate ≥ 0.9) |
| `primary_flaky_50` | Primary fails ~50% of calls; circuit should occasionally trip (3 failures in a row) and oscillate, with a mix of `primary` and `fallback` routes, and — unlike the 100%-fail scenario — should sometimes fully recover (CLOSE) once primary happens to succeed on a probe. | availability 0.99, fallback_success_rate 0.9762, circuit_open_count 4, recovery_time_ms **2404.34 ms** — the circuit tripped 4 times and, this run, did complete at least one full OPEN→HALF_OPEN→CLOSED cycle (probe happened to land on a lucky primary success), unlike the deterministic-failure scenario above. | ✅ pass |
| `all_healthy` | Both providers healthy at baseline fail rates (25%/5%); circuit may still open occasionally on a random bad streak, but should recover fast; nearly all traffic served via primary or cache. | availability 0.99, error_rate 0.01, circuit_open_count 1, recovery_time_ms 2236.45 ms — one brief trip and a fast, clean recovery, consistent with the low baseline fail rate. | ✅ pass |
| `both_degraded_70` (added) | Both providers fail 70% simultaneously — this should push a meaningful fraction of requests to `static_fallback` and is expected to *not* meet a normal-traffic SLO; the interesting check is that the system degrades gracefully (returns the static message + `error` field) instead of throwing or hanging. | availability 0.19, error_rate 0.81, fallback_success_rate 0.0122, circuit_open_count 2 (opened, then kept re-opening on failed probes — same never-closes mechanism as `primary_timeout_100`, recovery_time_ms null). No exceptions escaped `gateway.complete()`; every failed request still returned a well-formed `GatewayResponse` with `route="static_fallback"` and a non-null `error`. | ❌ fails the 50%-availability bar set for this scenario (0.19 < 0.5) — the meaningful pass signal here is "no crash, graceful static fallback for every request," which held; see §8 for why a hard availability bar on an intentionally worst-case scenario is itself debatable. |

## 8. Failure analysis

**Weakness: chaos runs are not seeded, so scenario pass/fail and `recovery_time_ms` are not statistically reproducible run-to-run**, only *structurally* reproducible (same JSON schema, same keys, plausible ranges). `FakeLLMProvider.complete()` uses bare `random.random()` / `random.randint()` with no seed, and `run_scenario()` draws `random.choice(queries)` per request. Concretely, across the several trial runs captured while building this report:
- `both_degraded_70`'s pass/fail bar (availability ≥ 0.5) flipped between runs purely from random variance — it failed in the memory-backend run reported above (§4, availability 0.19) but *passed* in a same-config Redis-backend run ([`reports/metrics_redis.json`](metrics_redis.json), availability 0.9325 with all 4 scenarios showing "pass" — cache hits from repeated queries bypassed the degraded providers entirely often enough to clear the bar that run).
- `primary_timeout_100`'s `recovery_time_ms` reliably comes back `null` across runs — this is *guaranteed*, not random, since the failing provider can never pass a HALF_OPEN probe while its 100% override is active — but `primary_flaky_50` and `all_healthy` sometimes show a real recovery time (this run: 2404 ms / 2236 ms) and sometimes `null` (an earlier trial), depending on whether the 100-request window happened to include a full open→probe→success cycle.

**Proposed fix:** seed the RNG per scenario (derived deterministically from `scenario.name`, so different scenarios still explore different randomness but any single scenario is reproducible run-to-run), or — closer to real chaos-engineering practice — run each scenario N times and report mean ± stddev instead of a single-sample point estimate, so pass/fail is based on a confidence interval rather than one noisy draw. Separately, `calculate_recovery_time_ms()` should distinguish "no circuit ever opened" from "circuit opened but never closed within the observation window" (currently both report `None`) — e.g. surface `recovery_time_ms: null, recovery_status: "never_recovered"` vs `"never_tripped"` — so operators reading the dashboard don't have to guess which case they're in.

**Deterministic proof the recovery mechanism itself works correctly** (isolated unit-level example, `CircuitBreaker("primary", failure_threshold=3, reset_timeout_seconds=2)`, 3 failures then a 2.05s sleep then one successful call):

```json
[
  {"from": "closed", "to": "open", "reason": "failure_threshold_reached", "ts": 1787849982.83},
  {"from": "open", "to": "half_open", "reason": "reset_timeout_elapsed", "ts": 1787849984.89},
  {"from": "half_open", "to": "closed", "reason": "probe_success", "ts": 1787849984.89}
]
```
Recovery time ≈ (1787849984.89 − 1787849982.83) × 1000 ≈ **2055 ms**, matching the configured `reset_timeout_seconds=2` almost exactly — `calculate_recovery_time_ms()` is correct; it's just data-starved in scenarios where the failure condition never actually clears (see above).

**False-hit guardrail evidence** (also referenced in §2): `similarity("Summarize refund policy for 2024 deadline", "Summarize refund policy for 2026 deadline")` scores **0.915** — high enough to look like a cache hit under a naive threshold — but `_looks_like_false_hit()` correctly detects the differing 4-digit year and the cache returns `(None, 0.915)` with a log entry `{"reason": "date_or_number_mismatch", ...}` instead of silently serving the 2024 answer to a 2026 query.

## 9. Next steps

1. Seed or repeat-and-aggregate chaos scenario runs (see §8) so `recovery_time_ms` and scenario pass/fail are statistically reproducible for grading, not just schema-reproducible.
2. Implement the stretch goal of Redis-backed circuit-breaker counters (`INCR`/`EXPIRE`) so breaker state — not just cache state — is shared across multiple gateway instances, closing the remaining single-point-of-inconsistency between instances.
3. Add cost-aware routing: once cumulative `estimated_cost` crosses a configured budget threshold, route to the cheaper `backup` provider first or go cache-only, rather than always preferring `primary` regardless of cost pressure.

## Reproducing these numbers

```bash
python3.11 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
docker compose up -d
make test                                                     # 35 passed, 7 xpassed
python scripts/run_chaos_detailed.py --config configs/default.yaml \
    --out reports/metrics.json --detail-out reports/metrics_by_scenario.json
python scripts/run_chaos_detailed.py --config configs/no_cache.yaml \
    --out reports/metrics_no_cache.json --detail-out reports/metrics_no_cache_by_scenario.json
python scripts/run_chaos_detailed.py --config configs/redis_cache.yaml \
    --out reports/metrics_redis.json --detail-out reports/metrics_redis_by_scenario.json
docker compose exec redis redis-cli KEYS "rl:cache:*"
```
(`scripts/run_chaos_detailed.py` is a thin wrapper I added around `run_scenario()`/`write_json`/`write_csv` to also dump the per-scenario breakdown used in §4 and §7; `make run-chaos` still works unchanged for the grader's exact required command and writes the same `reports/metrics.json` — only `make report`, per the note at the top, should not be re-run over this file.)
