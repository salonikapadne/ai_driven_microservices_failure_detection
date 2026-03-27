# simulate.py — Run fake log scenarios through the LangGraph agent
#
# Uses REAL LLM (reads credentials from .env) and DRY-RUN Docker tools.
# No RabbitMQ, no HTTP server — just the agent logic end-to-end.
#
# Run:
#   python simulate.py                   # all scenarios
#   python simulate.py --scenario 1      # single scenario by number
#   python simulate.py --dry-llm         # use FakeLLM (no API key needed)

import argparse
import json
import logging
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,   # suppress verbose library logs during simulation
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logging.getLogger("agent").setLevel(logging.INFO)
logging.getLogger("tools").setLevel(logging.INFO)

# ── imports ───────────────────────────────────────────────────────────────────
from ai_engine.state import StateManager, IncidentStatus
from ai_engine.tools import ToolManager
from ai_engine.agent import Agent

# ── fake LLM (no API key required) ───────────────────────────────────────────
from ai_engine.state import KNOWN_SERVICES

class FakeLLM:
    """Deterministic stub — returns the correct action for each failure type."""
    def generate(self, prompt: str, temperature: float = 0.2) -> str:
        import re
        ft_match = re.search(r"Failure Type\s*:\s*(\S+)", prompt)
        failure_type = ft_match.group(1) if ft_match else ""
        action_map = {
            "db_app_escalate": "escalate",
            "db_down":      "restart_database",
            "service_down": "restart_service",
            "error_logs":   "restart_service",
        }
        action = action_map.get(failure_type, "escalate")
        service = next((s for s in KNOWN_SERVICES if s in prompt), "unknown-service")
        return json.dumps({
            "action": action,
            "service": service,
            "error_summary": f"[FakeLLM] Detected {failure_type} on {service}.",
            "root_cause": f"[FakeLLM] Root cause for {failure_type}.",
            "fix_explanation": f"[FakeLLM] '{action}' will restore the service.",
            "reasoning": "[FakeLLM] Deterministic stub decision.",
            "confidence": "high",
            "alternative": "escalate",
        })


# ── fake log scenarios ────────────────────────────────────────────────────────
SCENARIOS = [
    # ── DB_DOWN ──────────────────────────────────────────────────────────────
    {
        "name": "DB_DOWN — payment-service (postgres connection refused)",
        "service": "payment-service",
        "container_status": "running",
        "exit_code": 0,
        "log_lines": [
            "2026-03-27 14:35:18 INFO  Starting database connection pool",
            "2026-03-27 14:35:19 ERROR Connection refused: postgres://db:5432",
            "2026-03-27 14:35:20 ERROR Retry attempt 1/3 failed",
            "2026-03-27 14:35:21 ERROR Retry attempt 2/3 failed",
            "2026-03-27 14:35:22 CRITICAL Circuit breaker OPEN — returning 503",
        ],
        "expected_type":   "db_down",
        "expected_action": "restart_database",
    },
    {
        "name": "DB_DOWN — order-service (mysql timeout)",
        "service": "order-service",
        "container_status": "running",
        "exit_code": 0,
        "log_lines": [
            "2026-03-27 15:10:01 ERROR Connection timeout: mysql://db:3306",
            "2026-03-27 15:10:02 ERROR Database connection pool exhausted",
            "2026-03-27 15:10:03 ERROR All retry attempts failed",
        ],
        "expected_type":   "db_down",
        "expected_action": "restart_database",
    },
    {
        "name": "DB_DOWN — user-service (redis unreachable)",
        "service": "user-service",
        "container_status": "running",
        "exit_code": 0,
        "log_lines": [
            "2026-03-27 16:00:05 ERROR Connection refused: redis://cache:6379",
            "2026-03-27 16:00:06 ERROR Session store unavailable",
            "2026-03-27 16:00:07 WARN  Falling back to in-memory sessions",
        ],
        "expected_type":   "db_down",
        "expected_action": "restart_database",
    },
    {
        "name": "DB_APP_ESCALATE — hil-db-demo (simulated migration failure → human)",
        "service": "hil-db-demo",
        "container_status": "running",
        "exit_code": 0,
        "log_lines": [
            "2026-03-27 21:00:00 INFO  Starting migration runner",
            "[HIL_DB_DEMO] 2026-03-27 21:00:01 FATAL schema migration checksum mismatch — database state cannot be reconciled automatically",
            "[HIL_DB_DEMO] human escalation required: run manual migration repair",
        ],
        "expected_type":   "db_app_escalate",
        "expected_action": "escalate",
    },
    # ── SERVICE_DOWN ─────────────────────────────────────────────────────────
    {
        "name": "SERVICE_DOWN — order-service (OOM, exit 137)",
        "service": "order-service",
        "container_status": "exited",
        "exit_code": 137,
        "log_lines": [
            "2026-03-27 14:40:12 WARN  Memory usage: 95%",
            "2026-03-27 14:40:13 ERROR Unable to allocate 512MB",
            "2026-03-27 14:40:14 ERROR Out of memory: OOM Killer invoked",
            "2026-03-27 14:40:15 ERROR Container exited with code 137",
        ],
        "expected_type":   "service_down",
        "expected_action": "restart_service",
    },
    {
        "name": "SERVICE_DOWN — gateway-service (segfault, exit 139)",
        "service": "gateway-service",
        "container_status": "exited",
        "exit_code": 139,
        "log_lines": [
            "2026-03-27 17:22:00 ERROR Segmentation fault in worker thread",
            "2026-03-27 17:22:01 ERROR Core dump written to /tmp/core.5678",
            "2026-03-27 17:22:02 ERROR Container exited with code 139",
        ],
        "expected_type":   "service_down",
        "expected_action": "restart_service",
    },
    {
        "name": "SERVICE_DOWN — payment-service (port conflict, exit 1)",
        "service": "payment-service",
        "container_status": "exited",
        "exit_code": 1,
        "log_lines": [
            "2026-03-27 18:05:00 ERROR Failed to bind port 8080: address already in use",
            "2026-03-27 18:05:01 ERROR Service terminated unexpectedly",
        ],
        "expected_type":   "service_down",
        "expected_action": "restart_service",
    },
    # ── ERROR_LOGS ───────────────────────────────────────────────────────────
    {
        "name": "ERROR_LOGS — user-service (NullPointerException)",
        "service": "user-service",
        "container_status": "running",
        "exit_code": 0,
        "log_lines": [
            "2026-03-27 14:45:30 ERROR NullPointerException in TokenValidator",
            "2026-03-27 14:45:30 ERROR   at com.auth.TokenValidator.verify:145",
            "2026-03-27 14:45:31 ERROR Users endpoint returning 500 errors",
            "2026-03-27 14:45:32 ERROR Failed to process 150 requests",
        ],
        "expected_type":   "error_logs",
        "expected_action": "restart_service",
    },
    {
        "name": "ERROR_LOGS — gateway-service (unhandled panic)",
        "service": "gateway-service",
        "container_status": "running",
        "exit_code": 0,
        "log_lines": [
            "2026-03-27 19:12:00 ERROR Unhandled panic in route /api/v2/checkout",
            "2026-03-27 19:12:01 ERROR Stack trace: goroutine 47 [running]",
            "2026-03-27 19:12:02 WARN  Error rate spiked to 12%",
        ],
        "expected_type":   "error_logs",
        "expected_action": "restart_service",
    },
    {
        "name": "ERROR_LOGS — order-service (KeyError traceback)",
        "service": "order-service",
        "container_status": "running",
        "exit_code": 0,
        "log_lines": [
            "2026-03-27 20:01:00 ERROR Traceback (most recent call last):",
            "2026-03-27 20:01:00 ERROR   File 'order_handler.py', line 88",
            "2026-03-27 20:01:01 ERROR   KeyError: 'shipping_address'",
            "2026-03-27 20:01:02 WARN  5xx rate above threshold",
        ],
        "expected_type":   "error_logs",
        "expected_action": "restart_service",
    },
]


# ── display helpers ───────────────────────────────────────────────────────────
SEP  = "-" * 72
SEP2 = "=" * 72

def _print_scenario_header(idx: int, total: int, name: str) -> None:
    print(f"\n{SEP2}")
    print(f"  Scenario {idx}/{total}: {name}")
    print(SEP2)

def _print_input(scenario: dict) -> None:
    print(f"  {'SERVICE':<18} {scenario['service']}")
    print(f"  {'CONTAINER STATUS':<18} {scenario['container_status']}")
    print(f"  {'EXIT CODE':<18} {scenario['exit_code']}")
    print(f"  {'LOGS':<18}")
    for line in scenario["log_lines"]:
        print(f"               {line}")

def _print_classify(result: dict) -> None:
    print(f"\n  [CLASSIFY]")
    print(f"    failure_type  : {result['failure_type']}")
    print(f"    error_keyword : {result['error_keyword']}")
    print(f"    severity      : {result['severity']}")
    print(f"    tags          : {result['tags']}")

def _print_decision(result: dict) -> None:
    d = result["decision"]
    print(f"\n  [LLM DECISION]")
    print(f"    action        : {d.get('action')}")
    print(f"    confidence    : {d.get('confidence')}")
    print(f"    error_summary : {d.get('error_summary', '')}")
    print(f"    root_cause    : {d.get('root_cause', '')}")
    print(f"    fix_expl.     : {d.get('fix_explanation', '')}")
    print(f"    reasoning     : {d.get('reasoning', '')}")
    print(f"    alternative   : {d.get('alternative', '')}")
    print(f"    llm_provider  : {result['active_provider']}")

def _print_tool(result: dict) -> None:
    tr = result.get("tool_result", {})
    print(f"\n  [TOOL EXECUTION]")
    print(f"    tool          : {tr.get('tool', 'n/a')}")
    print(f"    success       : {tr.get('success')}")
    print(f"    duration      : {tr.get('duration_seconds', 0):.2f}s")
    print(f"    message       : {tr.get('message', '')}")
    if tr.get("error"):
        print(f"    error         : {tr['error']}")

def _print_outcome(result: dict, scenario: dict) -> str:
    healed       = result["healed"]
    final_status = result["final_status"]
    action       = result["decision"].get("action", "")
    expected     = scenario["expected_action"]
    type_match   = result["failure_type"] == scenario["expected_type"]
    action_match = action == expected

    verdict = "PASS" if (type_match and action_match) else "FAIL"
    icon    = "[PASS]" if verdict == "PASS" else "[FAIL]"

    print(f"\n  [OUTCOME]")
    print(f"    incident_id   : {result['incident_id']}")
    print(f"    final_status  : {final_status}")
    print(f"    healed        : {healed}")
    print(f"    type check    : {'OK  ' if type_match  else 'FAIL'} "
          f"(expected={scenario['expected_type']}, got={result['failure_type']})")
    print(f"    action check  : {'OK  ' if action_match else 'FAIL'} "
          f"(expected={expected}, got={action})")
    print(f"\n  {icon}")
    return verdict


# ── main runner ───────────────────────────────────────────────────────────────
def build_agent(use_fake_llm: bool) -> Agent:
    sm = StateManager()
    tm = ToolManager(sm, dry_run=True)   # always dry-run; no real Docker ops

    if use_fake_llm:
        a = Agent.__new__(Agent)
        a.state_manager   = sm
        a.tool_manager    = tm
        a.primary         = "fake"
        a.fallbacks       = []
        a.llm             = FakeLLM()
        a.active_provider = "fake"
        a.graph           = a._build_graph()
        print("  [INFO] Using FakeLLM (no API key required)\n")
    else:
        provider  = os.getenv("LLM_PROVIDER", "local")
        fallbacks = [p.strip() for p in os.getenv("LLM_FALLBACKS", "gemini,openai").split(",") if p.strip()]
        a = Agent(sm, tm, llm_provider=provider, fallback_providers=fallbacks)
        print(f"  [INFO] Using real LLM provider: {a.active_provider}\n")

    return a


def run_simulation(scenarios: list, use_fake_llm: bool) -> None:
    print(f"\n{SEP2}")
    print(f"  AI AGENT SIMULATION — LangGraph Pipeline")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(SEP2)

    agent = build_agent(use_fake_llm)
    total   = len(scenarios)
    results = []

    for idx, scenario in enumerate(scenarios, 1):
        _print_scenario_header(idx, total, scenario["name"])
        _print_input(scenario)

        result = agent.run(
            service=scenario["service"],
            log_lines=scenario["log_lines"],
            container_status=scenario["container_status"],
            exit_code=scenario["exit_code"],
            timestamp=datetime.now().isoformat(),
        )

        _print_classify(result)
        _print_decision(result)
        _print_tool(result)
        verdict = _print_outcome(result, scenario)
        results.append((scenario["name"], verdict))

    # ── summary ───────────────────────────────────────────────────────────────
    passed = sum(1 for _, v in results if v == "PASS")
    failed = total - passed

    print(f"\n{SEP2}")
    print(f"  SUMMARY")
    print(SEP2)
    for name, verdict in results:
        icon = "[PASS]" if verdict == "PASS" else "[FAIL]"
        print(f"  {icon}  {name}")
    print(SEP)
    print(f"  Total: {total}  |  Passed: {passed}  |  Failed: {failed}")
    print(SEP2 + "\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Agent Fake-Log Simulation")
    parser.add_argument(
        "--scenario", type=int, default=None,
        help="Run only this scenario number (1-based). Default: run all.",
    )
    parser.add_argument(
        "--dry-llm", action="store_true",
        help="Use FakeLLM (no API key needed). Default: use real LLM from .env.",
    )
    args = parser.parse_args()

    if args.scenario is not None:
        if not (1 <= args.scenario <= len(SCENARIOS)):
            print(f"Error: --scenario must be between 1 and {len(SCENARIOS)}")
            sys.exit(1)
        chosen = [SCENARIOS[args.scenario - 1]]
    else:
        chosen = SCENARIOS

    run_simulation(chosen, use_fake_llm=args.dry_llm)
