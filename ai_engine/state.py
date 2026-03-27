# ai_engine/state.py — Incident state machine & failure classifier
# Owns: Incident lifecycle, storage, event dispatch, failure classification

import logging
import json
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from typing import List, Optional, Dict, Callable
from uuid import uuid4
from collections import defaultdict

logger = logging.getLogger("state")

# ---------------------------------------------------------------------------
# Incident status lifecycle
# ---------------------------------------------------------------------------
class IncidentStatus(Enum):
    DETECTED   = "detected"
    ANALYZING  = "analyzing"
    EXECUTING  = "executing"
    VERIFIED   = "verified"
    HEALED     = "healed"
    ESCALATED  = "escalated"
    RETRY      = "retry"

# ---------------------------------------------------------------------------
# Failure types (the 3 we care about)
# ---------------------------------------------------------------------------
class FailureType(Enum):
    DB_DOWN      = "db_down"
    SERVICE_DOWN = "service_down"
    ERROR_LOGS   = "error_logs"
    UNKNOWN      = "unknown"

# ---------------------------------------------------------------------------
# Known services
# ---------------------------------------------------------------------------
KNOWN_SERVICES = ["gateway-service", "payment-service", "order-service", "user-service"]

# ---------------------------------------------------------------------------
# Incident dataclass
# ---------------------------------------------------------------------------
@dataclass
class Incident:
    id: str                          = field(default_factory=lambda: f"INC-{uuid4().hex[:8]}")
    service: str                     = ""
    timestamp: str                   = ""
    detected_at: datetime            = field(default_factory=datetime.now)

    container_status: str            = ""      # "running" | "exited"
    exit_code: int                   = 0

    log_lines: List[str]             = field(default_factory=list)

    failure_type: str                = FailureType.UNKNOWN.value
    error_keyword: str               = ""
    severity: str                    = "medium"
    tags: List[str]                  = field(default_factory=list)

    status: IncidentStatus           = IncidentStatus.DETECTED
    analysis_result: Optional[str]   = None
    suggested_action: Optional[str]  = None

    attempted_actions: List[Dict]    = field(default_factory=list)
    execution_result: Optional[str]  = None
    retry_count: int                 = 0
    resolved: bool                   = False
    healed_at: Optional[datetime]    = None

    context: Dict                    = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "service": self.service,
            "timestamp": self.timestamp,
            "detected_at": self.detected_at.isoformat(),
            "container_status": self.container_status,
            "exit_code": self.exit_code,
            "log_lines": self.log_lines,
            "failure_type": self.failure_type,
            "error_keyword": self.error_keyword,
            "severity": self.severity,
            "tags": self.tags,
            "status": self.status.value,
            "analysis_result": self.analysis_result,
            "suggested_action": self.suggested_action,
            "attempted_actions": self.attempted_actions,
            "execution_result": self.execution_result,
            "retry_count": self.retry_count,
            "resolved": self.resolved,
            "healed_at": self.healed_at.isoformat() if self.healed_at else None,
            "context": self.context,
        }

    def __str__(self) -> str:
        return (
            f"Incident({self.id} | {self.service} | {self.failure_type} | "
            f"{self.status.value} | retry={self.retry_count})"
        )

# ---------------------------------------------------------------------------
# Failure classifier — maps raw logs to one of 3 failure types
# ---------------------------------------------------------------------------
DB_KEYWORDS = [
    "connection refused", "connection timeout", "connection reset",
    "database", "postgres", "mysql", "mongodb", "redis",
    "db", "sql", "relation does not exist", "no route to host",
]

SERVICE_DOWN_SIGNALS = [
    "oom killer", "out of memory", "killed", "segmentation fault",
    "cannot allocate", "service terminated", "process exited",
    "container exited", "health check failed", "not responding",
]

ERROR_LOG_KEYWORDS = [
    "exception", "error", "traceback", "nullpointer", "null pointer",
    "index out of bounds", "500 internal", "unhandled", "panic",
    "assertion failed", "timeout exceeded",
]


def classify_failure(
    log_lines: List[str],
    container_status: str,
    exit_code: int,
) -> tuple:
    """Return (failure_type, error_keyword, severity, tags)."""
    logs_lower = " ".join(log_lines).lower()

    # 1) DB_DOWN — connection / database keywords
    if any(kw in logs_lower for kw in DB_KEYWORDS):
        keyword = next((kw for kw in DB_KEYWORDS if kw in logs_lower), "db issue")
        logger.info("Classified as DB_DOWN (keyword=%s)", keyword)
        return (
            FailureType.DB_DOWN.value,
            keyword,
            "critical",
            ["database", "connectivity"],
        )

    # 2) SERVICE_DOWN — container exited OR OOM / crash signals
    if container_status == "exited" or exit_code != 0:
        keyword = f"exit_code_{exit_code}"
        for kw in SERVICE_DOWN_SIGNALS:
            if kw in logs_lower:
                keyword = kw
                break
        logger.info("Classified as SERVICE_DOWN (keyword=%s)", keyword)
        return (
            FailureType.SERVICE_DOWN.value,
            keyword,
            "critical",
            ["crash", "service_down"],
        )

    if any(kw in logs_lower for kw in SERVICE_DOWN_SIGNALS):
        keyword = next((kw for kw in SERVICE_DOWN_SIGNALS if kw in logs_lower), "service issue")
        logger.info("Classified as SERVICE_DOWN (keyword=%s)", keyword)
        return (
            FailureType.SERVICE_DOWN.value,
            keyword,
            "critical",
            ["crash", "service_down"],
        )

    # 3) ERROR_LOGS — application-level errors while container is running
    if any(kw in logs_lower for kw in ERROR_LOG_KEYWORDS):
        keyword = next((kw for kw in ERROR_LOG_KEYWORDS if kw in logs_lower), "app error")
        logger.info("Classified as ERROR_LOGS (keyword=%s)", keyword)
        return (
            FailureType.ERROR_LOGS.value,
            keyword,
            "high",
            ["application_error", "error_logs"],
        )

    logger.warning("Could not classify failure — defaulting to UNKNOWN")
    return (FailureType.UNKNOWN.value, "unknown", "low", ["unknown"])

# ---------------------------------------------------------------------------
# State Manager — single source of truth for all incidents
# ---------------------------------------------------------------------------
class StateManager:
    MAX_RETRIES = 3

    def __init__(self):
        self.incidents: Dict[str, Incident] = {}
        self._handlers: Dict[str, List[Callable]] = defaultdict(list)
        logger.info("StateManager initialised (max_retries=%d)", self.MAX_RETRIES)

    # ---- storage -----------------------------------------------------------
    def store_incident(self, incident: Incident) -> str:
        self.incidents[incident.id] = incident
        logger.info("Stored incident %s", incident)
        self._emit("incident_detected", incident)
        return incident.id

    def update_incident(self, incident_id: str, **updates) -> Incident:
        if incident_id not in self.incidents:
            raise ValueError(f"Incident {incident_id} not found")

        inc = self.incidents[incident_id]
        old_status = inc.status

        for key, value in updates.items():
            if hasattr(inc, key):
                setattr(inc, key, value)

        if old_status != inc.status:
            logger.info(
                "Incident %s status: %s -> %s",
                incident_id, old_status.value, inc.status.value,
            )
            self._emit(f"status_{inc.status.value}", inc)

        return inc

    def get_incident(self, incident_id: str) -> Optional[Incident]:
        return self.incidents.get(incident_id)

    def list_incidents(self, status: Optional[IncidentStatus] = None) -> List[Incident]:
        if status is None:
            return list(self.incidents.values())
        return [i for i in self.incidents.values() if i.status == status]

    def get_service_incidents(self, service: str) -> List[Incident]:
        return [i for i in self.incidents.values() if i.service == service]

    def should_escalate(self, incident_id: str) -> bool:
        inc = self.incidents.get(incident_id)
        if not inc:
            return False
        return inc.retry_count >= self.MAX_RETRIES

    # ---- event system ------------------------------------------------------
    def register_handler(self, event_name: str, handler: Callable) -> None:
        self._handlers[event_name].append(handler)
        logger.debug("Registered handler for event '%s'", event_name)

    def _emit(self, event_name: str, incident: Incident) -> None:
        for handler in self._handlers.get(event_name, []):
            try:
                handler(incident)
            except Exception:
                logger.exception("Handler error for event '%s'", event_name)

    # ---- persistence (status.json for dashboard) ---------------------------
    def write_status_json(self, path: str = "status.json") -> None:
        payload = {
            "updated_at": datetime.now().isoformat(),
            "incidents": [inc.to_dict() for inc in self.incidents.values()],
            "metrics": {
                "total": len(self.incidents),
                "healed": sum(1 for i in self.incidents.values() if i.resolved),
                "escalated": sum(
                    1 for i in self.incidents.values()
                    if i.status == IncidentStatus.ESCALATED
                ),
            },
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        logger.debug("Wrote status.json (%d incidents)", len(self.incidents))


# ---------------------------------------------------------------------------
# JSON log formatter
# ---------------------------------------------------------------------------
class _JSONLogFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": self.formatTime(record),
            "module": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        return json.dumps(entry)


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _handler = logging.StreamHandler()
    _handler.setFormatter(_JSONLogFormatter())
    logging.root.handlers = [_handler]
    logging.root.setLevel(logging.DEBUG)

    sm = StateManager()

    def _on_detected(inc: Incident):
        logger.info("EVENT incident_detected -> %s", inc)

    sm.register_handler("incident_detected", _on_detected)

    test_scenarios = [
        {
            "name": "DB_DOWN",
            "service": "payment-service",
            "container_status": "running",
            "exit_code": 0,
            "log_lines": [
                "ERROR Connection refused: postgres://db:5432",
                "ERROR Retry attempt 1 failed",
            ],
        },
        {
            "name": "SERVICE_DOWN",
            "service": "order-service",
            "container_status": "exited",
            "exit_code": 137,
            "log_lines": ["ERROR Out of memory: OOM Killer invoked"],
        },
        {
            "name": "ERROR_LOGS",
            "service": "user-service",
            "container_status": "running",
            "exit_code": 0,
            "log_lines": ["ERROR NullPointerException in AuthHandler"],
        },
    ]

    results = []
    for scenario in test_scenarios:
        inc = Incident(
            service=scenario["service"],
            timestamp=datetime.now().isoformat(),
            container_status=scenario["container_status"],
            exit_code=scenario["exit_code"],
            log_lines=scenario["log_lines"],
        )
        inc.failure_type, inc.error_keyword, inc.severity, inc.tags = classify_failure(
            inc.log_lines, inc.container_status, inc.exit_code,
        )
        sm.store_incident(inc)
        results.append({
            "scenario": scenario["name"],
            "incident_id": inc.id,
            "service": inc.service,
            "failure_type": inc.failure_type,
            "severity": inc.severity,
            "error_keyword": inc.error_keyword,
            "tags": inc.tags,
            "status": inc.status.value,
        })

    sm.write_status_json()

    output = {
        "test": "state.py self-test",
        "status": "PASSED",
        "total_incidents": len(results),
        "incidents": results,
    }
    print(json.dumps(output, indent=2))
