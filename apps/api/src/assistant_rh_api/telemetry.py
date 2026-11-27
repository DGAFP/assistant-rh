"""Low-cardinality Prometheus metrics and allowlisted technical diagnostics."""

import logging
import math
from collections.abc import Callable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from assistant_rh_api.core.observability import Observation

logger = logging.getLogger(__name__)
BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 240, 360)
STAGES = frozenset(("configuration", "query-processor", "retriever", "section-aggregator", "context-selector", "context-builder", "generator"))
FAILURES = frozenset(("timeout", "unavailable", "rate_limited", "rejected", "invalid_response", "circuit_open", "cancelled", "error"))
DB_REASONS = frozenset(
    (
        "pool_closed",
        "pool_checkout_timeout",
        "pool_queue_full",
        "query_cancelled",
        "connection_error",
        "admission_queue_full",
        "admission_timeout",
        "connection_check_timeout",
        "sql_error",
        "conflict",
    )
)
ROUTES = frozenset(("/healthz", "/v1/models", "/v1/chat/completions", "/v1/auth/groups", "/v1/auth/session", "/v1/auth/me"))


def route_name(path: str) -> str:
    if path in ROUTES:
        return path
    for prefix in ("/v1/documents/", "/v1/feedback"):
        if path.startswith(prefix):
            return prefix.rstrip("/")
    return "other"


class PoolCollector:
    """Read only bounded integer statistics; never connect or run SQL on scrape."""

    def __init__(self, statistics: Callable[[], dict[str, int]]) -> None:
        self.statistics = statistics

    def collect(self):
        stats = self.statistics()
        for key in ("pool_size", "pool_available", "pool_min", "pool_max", "requests_waiting", "search_active", "search_waiting", "search_capacity"):
            yield GaugeMetricFamily("assistant_rh_db_" + key, "Current pool/admission state", value=stats.get(key, 0))
        for key in ("requests_num", "requests_queued", "requests_errors", "connections_errors", "returns_bad"):
            yield CounterMetricFamily("assistant_rh_db_" + key, "Pool cumulative statistic", value=stats.get(key, 0))


class Telemetry:
    def __init__(self) -> None:
        # One registry per app, no global registration or identity labels.
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "assistant_rh_http_requests", "Finished HTTP requests", ("route", "method", "status", "outcome"), registry=self.registry
        )
        self.active = Gauge("assistant_rh_http_active", "In-flight HTTP requests", ("route",), registry=self.registry)
        self.duration = Histogram(
            "assistant_rh_http_duration_seconds",
            "Time through last successful body send",
            ("route", "outcome"),
            buckets=BUCKETS,
            registry=self.registry,
        )
        self.streams = Counter("assistant_rh_sse_streams", "SSE outcomes including failures after HTTP 200", ("outcome",), registry=self.registry)
        self.first_text = Histogram(
            "assistant_rh_sse_first_text_seconds",
            "Request to first pipeline text sent (excluding role/pings)",
            buckets=BUCKETS,
            registry=self.registry,
        )
        self.stages = Histogram(
            "assistant_rh_stage_duration_seconds",
            "Stage elapsed time, including failed/cancelled stages",
            ("stage", "outcome"),
            buckets=BUCKETS,
            registry=self.registry,
        )
        self.providers = Counter(
            "assistant_rh_provider_attempts",
            "Provider HTTP attempts, including retries and fallbacks",
            ("provider", "operation", "outcome"),
            registry=self.registry,
        )
        self.provider_duration = Histogram(
            "assistant_rh_provider_duration_seconds",
            "Provider HTTP attempt duration",
            ("provider", "operation", "outcome"),
            buckets=BUCKETS,
            registry=self.registry,
        )
        self.database_errors = Counter("assistant_rh_database_errors", "Fixed database error categories", ("reason",), registry=self.registry)
        self.stream_active = Gauge("assistant_rh_sse_active", "Admitted streaming workers", registry=self.registry)
        self.stream_capacity = Gauge("assistant_rh_sse_capacity", "Streaming worker capacity", registry=self.registry)

    def observation(self, request_id: str, event: Observation) -> None:
        seconds = event.seconds if math.isfinite(event.seconds) and event.seconds >= 0 else 0
        if event.kind == "stage":
            name = event.name if event.name in STAGES else "other"
            outcome = event.outcome if event.outcome in ("ok", "failed", "cancelled") else "failed"
            self.stages.labels(name, outcome).observe(seconds)
            provider = "none"
        elif event.kind == "provider":
            name = event.name if event.name in ("chat", "embeddings", "rerank") else "other"
            provider = event.provider if event.provider in ("albert", "scaleway") else "other"
            outcome = event.outcome if event.outcome in FAILURES or event.outcome == "ok" else "error"
            self.providers.labels(provider, name, outcome).inc()
            self.provider_duration.labels(provider, name, outcome).observe(seconds)
        else:
            name = "database"
            provider = "none"
            outcome = event.outcome if event.outcome in DB_REASONS else "sql_error"
            self.database_errors.labels(outcome).inc()
        logger.info(
            "api_diagnostic request_id=%s kind=%s name=%s provider=%s outcome=%s duration_ms=%d",
            request_id,
            event.kind,
            name,
            provider,
            outcome,
            int(seconds * 1000),
        )
