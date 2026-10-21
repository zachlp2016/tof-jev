"""Bounded service metrics shared by REST and MCP; never label with request data."""

from prometheus_client import Counter, Gauge, Histogram


class DecisionMetrics:
    def __init__(self, registry):
        self.completed = Counter(
            "tof_adapter_decisions_total",
            "Admitted decisions by outcome",
            ["consumer", "backend", "outcome"],
            registry=registry,
        )
        self.questions = Counter(
            "tof_adapter_questions_total",
            "Successfully completed questions",
            ["consumer", "backend"],
            registry=registry,
        )
        self.duration = Histogram(
            "tof_adapter_decision_duration_seconds",
            "Admitted decision latency",
            ["consumer", "backend"],
            registry=registry,
            buckets=(0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1, 2, 5, 15, 30, 90),
        )
        self.rejected = Counter(
            "tof_adapter_admission_rejections_total",
            "Immediate inference admission rejections",
            ["consumer", "scope"],
            registry=registry,
        )
        self.active = Gauge(
            "tof_adapter_active_decisions",
            "Active admitted decisions by consumer",
            ["consumer"],
            registry=registry,
        )
        self.global_active = Gauge(
            "tof_adapter_global_active_decisions",
            "Active decisions in the shared process pool",
            registry=registry,
        )
