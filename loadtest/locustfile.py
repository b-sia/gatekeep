"""Locust load-testing harness for gatekeep's own request-handling overhead.

Drives the gateway through the OpenAI-compatible /v1/chat/completions
endpoint using the in-app stub provider (gatekeep/providers/stub.py, gated
by LOADTEST_STUB_ENABLED - see loadtest/docker-compose.loadtest.yml) so
latency and cost are isolated from any real upstream. See
docs/superpowers/specs/2026-08-30-load-testing-harness-design.md §6.

Run one scenario at a time by naming its User class on the command line:

    locust -f loadtest/locustfile.py ThroughputUser --headless \\
        -u 400 -r 20 -t 5m --host http://localhost:8100 \\
        --csv loadtest/results/throughput

`just loadtest <ClassName>` wraps this (see justfile). ThroughputUser and
BreakingPointUser define their own LoadTestShape and ignore -u/-r; LatencyUser
and EnforcementUser use -u/-r directly.

Note: for a shape-driven scenario (ThroughputUser, BreakingPointUser),
Locust runs the shape's full stage list to completion before -t/--run-time
is even consulted (locust/main.py's start_automatic_run joins the shape
greenlet first) - -t above bounds LatencyUser/EnforcementUser runs, but has
no effect here. To stop a shape-driven run early, interrupt it with
Ctrl+C/SIGINT; Locust catches that around the shape join and shuts down
gracefully, printing the summary as usual.

Environment:
    TARGET_HOST: base URL of the gateway (default http://localhost:8100).
    LOADTEST_KEYS_PATH: path to bootstrap.py's keys.json (default loadtest/keys.json).
"""

from __future__ import annotations

import json
import os
import random
import sys
import uuid
from pathlib import Path

from locust import HttpUser, LoadTestShape, between, constant_pacing, task

# Locust picks the *first* non-abstract LoadTestShape subclass it finds in
# the locustfile as THE shape for the run, unconditionally - it does not
# consult which User class was named on the CLI (confirmed against
# locust 2.46.4's locust/util/load_locustfile.py:is_shape_class and
# locust/main.py:merge_locustfiles_content, where
# `shape_class = list(available_shape_classes.values())[0]`). Since this
# file defines two shapes (ThroughputShape, BreakingPointShape) alongside
# four User classes, without a guard, running `LatencyUser` or
# `EnforcementUser` headless would silently pick up ThroughputShape and
# ignore -u/-r, and BreakingPointShape would never be selectable at all.
# Mark each shape `abstract` (excluded from discovery - see
# LoadTestShapeMeta) unless its matching User class name is present on the
# command line. Only supports the single-class CLI invocation documented
# above; Web UI mode without a class argument sees no shape.
_SELECTED_CLASSES = set(sys.argv[1:])

# Cardinality guardrail (design doc §7): gateway_overhead_seconds,
# request_duration_seconds, and provider_duration_seconds are labeled by
# `model`, and the stub model string encodes latency/size/ITL - so every
# scenario below reuses exactly these two fixed model strings rather than
# generating parameterizations dynamically. Do not add more without also
# updating loadtest/README.md's Prometheus panel list.
MODEL_NON_STREAM = "stub/lat50-out200"
MODEL_STREAM = "stub/lat50-out200-itl5"

_KEYS_PATH = Path(os.environ.get("LOADTEST_KEYS_PATH", Path(__file__).parent / "keys.json"))
_CACHE_HIT_PROMPT = "the quick brown fox jumps over the lazy dog"


def _load_keys() -> dict[str, list[str]]:
    """Load the pool/budget key lists written by loadtest/bootstrap.py."""
    if not _KEYS_PATH.exists():
        raise FileNotFoundError(
            f"{_KEYS_PATH} not found - run `just loadtest-bootstrap` first "
            "(see loadtest/README.md)."
        )
    return json.loads(_KEYS_PATH.read_text())


_KEYS = _load_keys()


def _headers(raw_key: str) -> dict[str, str]:
    """Build the Authorization header for one gatekeep API key."""
    return {"Authorization": f"Bearer {raw_key}"}


def _body(*, model: str, cache_hit: bool, stream: bool) -> dict:
    """Build a chat-completion request body.

    A cache-hit request always sends the exact same prompt (stable hash, so
    repeated calls hit gatekeep's exact-response cache); a cache-miss
    request appends a fresh UUID so every call is a distinct, uncached
    prompt.
    """
    prompt = _CACHE_HIT_PROMPT if cache_hit else f"{_CACHE_HIT_PROMPT} {uuid.uuid4()}"
    return {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": stream}


class _StubTasks:
    """Shared task bodies mixed into every HttpUser class below.

    Each of the request shapes (non-stream/stream x cache-hit/cache-miss) is
    its own @task so Locust's per-task stats break them out individually.
    Weighted 3:1:2 (non-stream-miss : non-stream-hit : stream-miss) so the
    common non-streaming, cache-miss path dominates traffic while still
    exercising the cache and streaming paths every run.
    """

    def _post(self, *, cache_hit: bool, stream: bool) -> None:
        key = random.choice(self.keys)
        model = MODEL_STREAM if stream else MODEL_NON_STREAM
        body = _body(model=model, cache_hit=cache_hit, stream=stream)
        with self.client.post(
            "/v1/chat/completions",
            json=body,
            headers=_headers(key),
            stream=stream,
            catch_response=True,
        ) as response:
            if stream and response.status_code == 200:
                # Drain the SSE body so the connection's full duration - not
                # just headers - counts toward this task's response time.
                # A dropped connection mid-stream (e.g. the gateway closing
                # early under heavy load) raises requests.exceptions.
                # ChunkedEncodingError from inside iter_lines() -
                # catch_response only guards the initial request, not body
                # consumption inside this `with` block, so left unguarded
                # this would propagate as an unhandled traceback out of the
                # task instead of being recorded as a failed request.
                try:
                    for _ in response.iter_lines():
                        pass
                except Exception as exc:  # noqa: BLE001
                    response.failure(str(exc))

    @task(3)
    def non_stream_cache_miss(self) -> None:
        self._post(cache_hit=False, stream=False)

    @task(1)
    def non_stream_cache_hit(self) -> None:
        self._post(cache_hit=True, stream=False)

    @task(2)
    def stream_cache_miss(self) -> None:
        self._post(cache_hit=False, stream=True)


# Locust's UserMeta only auto-collects @task-decorated methods that live
# directly in a User subclass's own class body (or in a base that has
# already been through UserMeta, i.e. is itself a User/TaskSet) - installed
# locust 2.46.4 silently drops the tasks of a plain mixin class like
# _StubTasks otherwise (verified against locust.user.task.
# get_tasks_from_base_classes). Compute the weighted task list explicitly
# so it's picked up via `hasattr(base, "tasks")` when User subclasses below
# list _StubTasks as a base.
_StubTasks.tasks = [
    method
    for method in vars(_StubTasks).values()
    if hasattr(method, "locust_task_weight")
    for _ in range(method.locust_task_weight)
]


class ThroughputUser(_StubTasks, HttpUser):
    """Goal 1 - throughput/capacity: paired with ThroughputShape to find max
    sustainable RPS before p95 gateway overhead climbs or errors appear."""

    host = os.environ.get("TARGET_HOST", "http://localhost:8100")
    wait_time = between(0, 0)
    keys = _KEYS["pool"]


class ThroughputShape(LoadTestShape):
    """Step-ramp for ThroughputUser: 20->40->80->160 users doubling every
    30-90s, then +80 users every 30s up to 400.

    Watch gateway_overhead_seconds p95 and error rate in Grafana as each
    step lands; the step where p95 first climbs sharply is the practical
    capacity ceiling (see loadtest/README.md).
    """

    abstract = "ThroughputUser" not in _SELECTED_CLASSES

    stages = [
        {"duration": 30, "users": 20, "spawn_rate": 20},
        {"duration": 60, "users": 40, "spawn_rate": 20},
        {"duration": 90, "users": 80, "spawn_rate": 40},
        {"duration": 120, "users": 160, "spawn_rate": 80},
        {"duration": 150, "users": 240, "spawn_rate": 80},
        {"duration": 180, "users": 320, "spawn_rate": 80},
        {"duration": 210, "users": 400, "spawn_rate": 80},
    ]

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["duration"]:
                return stage["users"], stage["spawn_rate"]
        return None


class LatencyUser(_StubTasks, HttpUser):
    """Goal 2 - latency SLO: fixed, moderate concurrency; read
    gateway_overhead_seconds percentiles from Prometheus/Grafana during the
    run and compare against the draft SLOs in loadtest/README.md."""

    host = os.environ.get("TARGET_HOST", "http://localhost:8100")
    wait_time = constant_pacing(1)
    keys = _KEYS["pool"]


class BreakingPointUser(_StubTasks, HttpUser):
    """Goal 3 - breaking point: paired with BreakingPointShape, ramps well
    past ThroughputUser's ceiling to observe failure modes (connection-pool
    exhaustion, timeouts, 429s) rather than to find a clean capacity
    number."""

    host = os.environ.get("TARGET_HOST", "http://localhost:8100")
    wait_time = between(0, 0)
    keys = _KEYS["pool"]


class BreakingPointShape(LoadTestShape):
    """Faster, higher-ceiling step-ramp than ThroughputShape, for BreakingPointUser."""

    abstract = "BreakingPointUser" not in _SELECTED_CLASSES

    stages = [
        {"duration": 20, "users": 50, "spawn_rate": 50},
        {"duration": 40, "users": 200, "spawn_rate": 100},
        {"duration": 60, "users": 500, "spawn_rate": 150},
        {"duration": 80, "users": 1000, "spawn_rate": 200},
        {"duration": 100, "users": 1500, "spawn_rate": 200},
        {"duration": 120, "users": 2000, "spawn_rate": 200},
    ]

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["duration"]:
                return stage["users"], stage["spawn_rate"]
        return None


class EnforcementUser(HttpUser):
    """Goal 4 - enforcement under concurrency: saturates one key from a
    low-budget account (from bootstrap.py's "budget" pool) so the budget
    block fires at the predicted spend under real concurrent load - budget
    is enforced per-account, not per-key (rate-limit exactness is covered
    by BreakingPointUser instead - see design doc §6.4)."""

    host = os.environ.get("TARGET_HOST", "http://localhost:8100")
    wait_time = between(0, 0)

    def on_start(self) -> None:
        """Pin this simulated user to one randomly-chosen low-budget key for
        its whole run, so concurrent users still converge on saturating a
        small, known set of accounts."""
        self.key = random.choice(_KEYS["budget"])

    @task
    def hammer_low_budget_key(self) -> None:
        body = _body(model=MODEL_NON_STREAM, cache_hit=False, stream=False)
        self.client.post("/v1/chat/completions", json=body, headers=_headers(self.key))
