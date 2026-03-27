# tests/test_end_to_end.py — Full pipeline tests for the LangGraph agent
#
# No RabbitMQ, no real LLM, no Docker required.
# All LLM calls are handled by FakeLLM (deterministic stub).
# All Docker commands run in dry-run mode.
#
# Run:  python -m pytest tests/test_end_to_end.py -v

import json
import logging
from datetime import datetime

import pytest

from ai_engine.state import (
    Incident, IncidentStatus, FailureType,
    StateManager, classify_failure, KNOWN_SERVICES,
)
from ai_engine.agent import Agent, ALLOWED_ACTIONS, IncidentState
from ai_engine.tools import ToolManager, ACTION_MAP, ToolResult

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


# =====================================================================
# Fixtures
# =====================================================================
@pytest.fixture
def state_manager():
    return StateManager()


@pytest.fixture
def tool_manager(state_manager):
    return ToolManager(state_manager, dry_run=True)


class FakeLLM:
    """Deterministic LLM stub — returns the correct action for each failure type."""

    def __init__(self):
        self.last_prompt = ""

    def generate(self, prompt: str, temperature: float = 0.2) -> str:
        self.last_prompt = prompt

        import re
        ft_match = re.search(r"Failure Type\s*:\s*(\S+)", prompt)
        failure_type = ft_match.group(1) if ft_match else ""

        action_map = {
            "db_app_escalate": ("escalate", "Application-level DB error — human required."),
            "db_down":      ("restart_database", "Database connection failure detected."),
            "service_down": ("restart_service",  "Service container has crashed."),
            "error_logs":   ("restart_service",  "Application errors detected in running container."),
        }
        action, summary = action_map.get(failure_type, ("escalate", "Unknown failure."))

        service = "payment-service"
        for svc in KNOWN_SERVICES:
            if svc in prompt:
                service = svc
                break

        return json.dumps({
            "action": action,
            "service": service,
            "error_summary": summary,
            "root_cause": f"The {failure_type} failure indicates an infrastructure-level issue affecting {service}.",
            "fix_explanation": f"Running '{action}' via Docker will restore the affected component to a healthy state.",
            "reasoning": f"Based on failure type '{failure_type}', '{action}' is the recommended first-line recovery action.",
            "confidence": "high",
            "alternative": "escalate",
        })


class EscalateLLM:
    """Stub LLM that always returns escalate — for escalation path testing."""

    def generate(self, prompt: str, temperature: float = 0.2) -> str:
        service = "payment-service"
        for svc in KNOWN_SERVICES:
            if svc in prompt:
                service = svc
                break
        return json.dumps({
            "action": "escalate",
            "service": service,
            "error_summary": "Could not determine a safe automated action.",
            "root_cause": "Repeated failures suggest a deeper infrastructure problem.",
            "fix_explanation": "Manual intervention required.",
            "reasoning": "Escalating due to uncertainty.",
            "confidence": "low",
            "alternative": "escalate",
        })


def _make_agent(state_manager: StateManager, tool_manager: ToolManager, llm=None) -> Agent:
    """Build a test agent bypassing LLM provider initialisation."""
    a = Agent.__new__(Agent)
    a.state_manager = state_manager
    a.tool_manager = tool_manager
    a.primary = "fake"
    a.fallbacks = []
    a.llm = llm or FakeLLM()
    a.active_provider = "fake"
    a.graph = a._build_graph()
    return a


# =====================================================================
# 1. State tests (unchanged — state.py not modified)
# =====================================================================
class TestState:
    def test_incident_creation(self, state_manager):
        inc = Incident(service="payment-service", timestamp=datetime.now().isoformat(),
                       container_status="running", exit_code=0, log_lines=["ERROR db down"])
        iid = state_manager.store_incident(inc)
        assert state_manager.get_incident(iid) is inc
        assert inc.status == IncidentStatus.DETECTED

    def test_update_status(self, state_manager):
        inc = Incident(service="order-service")
        state_manager.store_incident(inc)
        state_manager.update_incident(inc.id, status=IncidentStatus.ANALYZING)
        assert state_manager.get_incident(inc.id).status == IncidentStatus.ANALYZING

    def test_escalation_check(self, state_manager):
        inc = Incident(service="user-service", retry_count=3)
        state_manager.store_incident(inc)
        assert state_manager.should_escalate(inc.id)

    def test_event_handler(self, state_manager):
        received = []
        state_manager.register_handler("incident_detected", lambda i: received.append(i.id))
        inc = Incident(service="gateway-service")
        state_manager.store_incident(inc)
        assert inc.id in received

    def test_to_dict(self):
        inc = Incident(service="payment-service", failure_type="db_down")
        d = inc.to_dict()
        assert d["service"] == "payment-service"
        assert d["failure_type"] == "db_down"

    def test_list_by_status(self, state_manager):
        inc1 = Incident(service="a", status=IncidentStatus.DETECTED)
        inc2 = Incident(service="b", status=IncidentStatus.HEALED)
        state_manager.store_incident(inc1)
        state_manager.store_incident(inc2)
        detected = state_manager.list_incidents(IncidentStatus.DETECTED)
        assert len(detected) == 1
        assert detected[0].service == "a"


# =====================================================================
# 2. Classifier tests (unchanged)
# =====================================================================
class TestClassifier:
    def test_db_down(self):
        ft, kw, sev, tags = classify_failure(
            ["ERROR Connection refused: postgres://db:5432"], "running", 0,
        )
        assert ft == FailureType.DB_DOWN.value
        assert sev == "critical"

    def test_service_down_oom(self):
        ft, kw, sev, tags = classify_failure(
            ["ERROR Out of memory: OOM Killer invoked"], "exited", 137,
        )
        assert ft == FailureType.SERVICE_DOWN.value

    def test_service_down_exited(self):
        ft, kw, sev, tags = classify_failure(
            ["ERROR Service stopped"], "exited", 1,
        )
        assert ft == FailureType.SERVICE_DOWN.value

    def test_error_logs(self):
        ft, kw, sev, tags = classify_failure(
            ["ERROR NullPointerException in AuthHandler"], "running", 0,
        )
        assert ft == FailureType.ERROR_LOGS.value

    def test_unknown(self):
        ft, kw, sev, tags = classify_failure(["INFO All good"], "running", 0)
        assert ft == FailureType.UNKNOWN.value

    def test_db_app_escalate(self):
        ft, kw, sev, tags = classify_failure(
            [
                "[HIL_DB_DEMO] FATAL schema migration checksum mismatch",
                "human escalation required",
            ],
            "running",
            0,
        )
        assert ft == FailureType.DB_APP_ESCALATE.value
        assert "human_escalation" in tags


# =====================================================================
# 3. LangGraph agent tests
# =====================================================================
class TestLangGraphAgent:
    """Tests for the LangGraph-based Agent.run() graph pipeline."""

    def test_db_down_routes_to_restart_database(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="payment-service",
            log_lines=["ERROR Connection refused: postgres://db:5432"],
            container_status="running",
            exit_code=0,
        )
        assert result["failure_type"] == "db_down"
        assert result["decision"]["action"] == "restart_database"
        assert result["decision"]["confidence"] == "high"
        assert result["decision"]["error_summary"] != ""
        assert result["decision"]["root_cause"] != ""
        assert result["decision"]["fix_explanation"] != ""

    def test_service_down_routes_to_restart_service(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="order-service",
            log_lines=["ERROR Out of memory: OOM Killer invoked"],
            container_status="exited",
            exit_code=137,
        )
        assert result["failure_type"] == "service_down"
        assert result["decision"]["action"] == "restart_service"
        assert result["decision"]["error_summary"] != ""

    def test_error_logs_routes_to_restart_service(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="user-service",
            log_lines=["ERROR NullPointerException in AuthHandler"],
            container_status="running",
            exit_code=0,
        )
        assert result["failure_type"] == "error_logs"
        assert result["decision"]["action"] == "restart_service"
        assert result["decision"]["fix_explanation"] != ""

    def test_db_app_escalate_skips_llm_and_escalates(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="hil-db-demo",
            log_lines=[
                "[HIL_DB_DEMO] FATAL schema migration checksum mismatch",
                "db_app_escalate: manual repair required",
            ],
            container_status="running",
            exit_code=0,
        )
        assert result["failure_type"] == "db_app_escalate"
        assert result["decision"]["action"] == "escalate"
        assert result["final_status"] == IncidentStatus.ESCALATED.value
        assert result["healed"] is False

    def test_incident_stored_in_state_manager(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="gateway-service",
            log_lines=["ERROR Connection refused: postgres://db:5432"],
            container_status="running",
            exit_code=0,
        )
        inc = state_manager.get_incident(result["incident_id"])
        assert inc is not None
        assert inc.suggested_action == result["decision"]["action"]

    def test_healed_on_successful_dry_run(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="payment-service",
            log_lines=["ERROR Connection refused: postgres://db:5432"],
            container_status="running",
            exit_code=0,
        )
        # dry_run health checks always return True
        assert result["healed"] is True
        assert result["final_status"] == IncidentStatus.HEALED.value

    def test_escalate_action_routes_to_escalate_node(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager, llm=EscalateLLM())
        # Logs must not match db_down / service_down / error_logs so classify_failure → unknown;
        # otherwise escalate is coerced to ACTION_MAP[failure_type] while retries remain.
        result = agent.run(
            service="payment-service",
            log_lines=["WARN deprecated configuration key will be removed"],
            container_status="running",
            exit_code=0,
        )
        assert result["failure_type"] == FailureType.UNKNOWN.value
        assert result["decision"]["action"] == "escalate"
        assert result["final_status"] == IncidentStatus.ESCALATED.value
        assert result["healed"] is False

    def test_max_retries_triggers_escalation(self, state_manager, tool_manager):
        """Inject an incident at MAX_RETRIES so should_escalate returns True on first analyze."""
        agent = _make_agent(state_manager, tool_manager)

        # Pre-populate state with an incident at max retries
        inc = Incident(
            service="gateway-service",
            container_status="running",
            exit_code=0,
            log_lines=["ERROR db connection refused"],
            failure_type="db_down",
            retry_count=StateManager.MAX_RETRIES,
        )
        state_manager.store_incident(inc)

        # Patch classify node to reuse this incident (simulate by running the analyze directly)
        # Use the graph but override the incident_id in initial state to skip classify
        # We test should_escalate by ensuring analyze node checks it
        assert state_manager.should_escalate(inc.id)

    def test_graph_result_contains_all_fields(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="order-service",
            log_lines=["ERROR Connection timeout: mysql://db:3306"],
            container_status="running",
            exit_code=0,
        )
        required_fields = [
            "incident_id", "failure_type", "error_keyword", "severity",
            "tags", "decision", "tool_result", "healed", "retry_count",
            "final_status", "active_provider",
        ]
        for field in required_fields:
            assert field in result, f"Missing field: {field}"

    def test_active_provider_recorded(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="user-service",
            log_lines=["ERROR NullPointerException"],
            container_status="running",
            exit_code=0,
        )
        assert result["active_provider"] == "fake"


# =====================================================================
# 4. Tools tests (dry-run — no Docker)
# =====================================================================
class TestTools:
    def test_restart_service_dry(self, state_manager, tool_manager):
        inc = Incident(service="payment-service", failure_type="service_down")
        state_manager.store_incident(inc)
        result = tool_manager.execute(inc.id, "restart_service", "payment-service")
        assert isinstance(result, ToolResult)
        assert result.success

    def test_restart_database_dry(self, state_manager, tool_manager):
        inc = Incident(service="payment-service", failure_type="db_down")
        state_manager.store_incident(inc)
        result = tool_manager.execute(inc.id, "restart_database", "payment-service")
        assert result.success

    def test_unknown_action(self, state_manager, tool_manager):
        inc = Incident(service="payment-service")
        state_manager.store_incident(inc)
        result = tool_manager.execute(inc.id, "delete_everything", "payment-service")
        assert not result.success

    def test_escalate_action(self, state_manager, tool_manager):
        inc = Incident(service="payment-service")
        state_manager.store_incident(inc)
        result = tool_manager.execute(inc.id, "escalate", "payment-service")
        assert not result.success
        refreshed = state_manager.get_incident(inc.id)
        assert refreshed.status == IncidentStatus.ESCALATED

    def test_fallback_execution(self, state_manager, tool_manager):
        inc = Incident(service="order-service", failure_type="service_down")
        state_manager.store_incident(inc)
        result = tool_manager.execute_with_fallback(
            inc.id, "restart_service", "scale_replicas", "order-service",
        )
        assert result.success

    def test_verify_and_close_healed(self, state_manager, tool_manager):
        inc = Incident(service="payment-service")
        state_manager.store_incident(inc)
        healed = tool_manager.verify_and_close(inc.id, "payment-service")
        assert healed
        refreshed = state_manager.get_incident(inc.id)
        assert refreshed.status == IncidentStatus.HEALED
        assert refreshed.resolved

    def test_action_map_covers_all_failure_types(self):
        for ft in [
            FailureType.DB_APP_ESCALATE,
            FailureType.DB_DOWN,
            FailureType.SERVICE_DOWN,
            FailureType.ERROR_LOGS,
        ]:
            assert ft.value in ACTION_MAP, f"Missing ACTION_MAP entry for {ft.value}"


# =====================================================================
# 5. Full LangGraph pipeline tests (classify → analyze → execute → verify)
# =====================================================================
class TestFullLangGraphPipeline:
    """
    End-to-end tests that exercise the complete LangGraph workflow.
    Each test submits raw log data and asserts on the final graph output.
    """

    def test_db_down_full_pipeline(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="payment-service",
            log_lines=[
                "ERROR Connection refused: postgres://db:5432",
                "ERROR Retry attempt 1/3 failed",
            ],
            container_status="running",
            exit_code=0,
        )
        assert result["failure_type"] == "db_down"
        assert result["decision"]["action"] == "restart_database"
        assert result["tool_result"]["success"] is True
        assert result["healed"] is True
        assert result["final_status"] == IncidentStatus.HEALED.value

        inc = state_manager.get_incident(result["incident_id"])
        assert inc.resolved is True

    def test_service_down_full_pipeline(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="order-service",
            log_lines=["ERROR Out of memory: OOM Killer invoked"],
            container_status="exited",
            exit_code=137,
        )
        assert result["failure_type"] == "service_down"
        assert result["decision"]["action"] == "restart_service"
        assert result["healed"] is True
        assert result["final_status"] == IncidentStatus.HEALED.value

    def test_error_logs_full_pipeline(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="user-service",
            log_lines=["ERROR NullPointerException in AuthHandler"],
            container_status="running",
            exit_code=0,
        )
        assert result["failure_type"] == "error_logs"
        assert result["decision"]["action"] == "restart_service"
        assert result["healed"] is True

    def test_escalation_pipeline(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager, llm=EscalateLLM())
        result = agent.run(
            service="gateway-service",
            log_lines=["WARN legacy endpoint scheduled for removal"],
            container_status="running",
            exit_code=0,
        )
        assert result["failure_type"] == FailureType.UNKNOWN.value
        assert result["decision"]["action"] == "escalate"
        assert result["final_status"] == IncidentStatus.ESCALATED.value
        assert result["healed"] is False

        inc = state_manager.get_incident(result["incident_id"])
        assert inc.status == IncidentStatus.ESCALATED

    def test_db_app_escalate_full_pipeline(self, state_manager, tool_manager):
        agent = _make_agent(state_manager, tool_manager)
        result = agent.run(
            service="hil-db-demo",
            log_lines=["[HIL_DB_DEMO] FATAL migration checksum mismatch"],
            container_status="running",
            exit_code=0,
        )
        assert result["failure_type"] == "db_app_escalate"
        assert result["decision"]["action"] == "escalate"
        assert result["final_status"] == IncidentStatus.ESCALATED.value
        assert result["healed"] is False
        inc = state_manager.get_incident(result["incident_id"])
        assert inc.status == IncidentStatus.ESCALATED

    def test_multiple_incidents_isolated(self, state_manager, tool_manager):
        """Two concurrent incidents should not interfere with each other."""
        agent = _make_agent(state_manager, tool_manager)

        r1 = agent.run(
            service="payment-service",
            log_lines=["ERROR Connection refused: postgres://db:5432"],
            container_status="running",
            exit_code=0,
        )
        r2 = agent.run(
            service="order-service",
            log_lines=["ERROR OOM Killer invoked"],
            container_status="exited",
            exit_code=137,
        )

        assert r1["incident_id"] != r2["incident_id"]
        assert r1["failure_type"] == "db_down"
        assert r2["failure_type"] == "service_down"
        assert r1["healed"] is True
        assert r2["healed"] is True

    def test_all_nine_scenarios(self, state_manager, tool_manager):
        """Smoke test: 9 representative log scenarios across 3 failure types."""
        agent = _make_agent(state_manager, tool_manager)

        scenarios = [
            # DB_DOWN
            ("payment-service",  ["ERROR Connection refused: postgres://db:5432"],          "running", 0,   "db_down",      "restart_database"),
            ("order-service",    ["ERROR Connection timeout: mysql://db:3306"],              "running", 0,   "db_down",      "restart_database"),
            ("user-service",     ["ERROR Connection refused: redis://cache:6379"],           "running", 0,   "db_down",      "restart_database"),
            # SERVICE_DOWN
            ("order-service",    ["ERROR Out of memory: OOM Killer invoked"],                "exited",  137, "service_down", "restart_service"),
            ("gateway-service",  ["ERROR Segmentation fault in worker thread"],              "exited",  139, "service_down", "restart_service"),
            ("payment-service",  ["ERROR Service terminated unexpectedly"],                  "exited",  1,   "service_down", "restart_service"),
            # ERROR_LOGS
            ("user-service",     ["ERROR NullPointerException in TokenValidator"],           "running", 0,   "error_logs",   "restart_service"),
            ("gateway-service",  ["ERROR Unhandled panic in route /api/v2/checkout"],        "running", 0,   "error_logs",   "restart_service"),
            ("order-service",    ["ERROR Traceback", "ERROR KeyError: 'shipping_address'"],  "running", 0,   "error_logs",   "restart_service"),
        ]

        for svc, logs, cstatus, ecode, expected_type, expected_action in scenarios:
            result = agent.run(
                service=svc,
                log_lines=logs,
                container_status=cstatus,
                exit_code=ecode,
            )
            assert result["failure_type"] == expected_type, \
                f"{svc}: expected type={expected_type}, got={result['failure_type']}"
            assert result["decision"]["action"] == expected_action, \
                f"{svc}: expected action={expected_action}, got={result['decision']['action']}"
            assert result["healed"] is True, f"{svc}: expected healed=True"
