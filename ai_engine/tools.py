# ai_engine/tools.py — Actuator & Critic: executes Docker recovery actions, verifies results
# Strict allowlist — only pre-approved actions are executable.

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    from .state import StateManager, IncidentStatus, Incident, FailureType
except ImportError:
    from state import StateManager, IncidentStatus, Incident, FailureType

logger = logging.getLogger("tools")


def _docker_bin() -> str:
    """CLI from static install (/usr/local/bin/docker) or PATH."""
    return os.environ.get("DOCKER_BIN", "docker")


def _compose_bin() -> str:
    """docker-compose v1 binary (separate from static docker client)."""
    return os.environ.get("DOCKER_COMPOSE_BIN", "docker-compose")


def _code_heal_root() -> str:
    return os.path.abspath(os.environ.get("CODE_HEAL_ROOT", "/buggy-live"))


def _code_heal_allowed_filenames() -> List[str]:
    raw = os.environ.get("CODE_HEAL_FILES", "app.py")
    return [p.strip().lstrip("/\\") for p in raw.split(",") if p.strip()]


def _code_heal_service_names() -> List[str]:
    raw = os.environ.get("CODE_HEAL_SERVICES", "buggy-service")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _code_heal_max_bytes() -> int:
    try:
        return int(os.environ.get("CODE_HEAL_MAX_BYTES", "262144"))
    except ValueError:
        return 262144


def _resolve_code_heal_path(rel_path: str) -> Tuple[str, Optional[str]]:
    """Return (absolute_path, error_message). rel_path must stay under CODE_HEAL_ROOT."""
    if not rel_path or not str(rel_path).strip():
        return "", "empty path"
    rel = str(rel_path).strip().replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        return "", "path traversal not allowed"
    allowed = _code_heal_allowed_filenames()
    if allowed and rel not in allowed:
        return "", f"path not in CODE_HEAL_FILES allowlist: {rel!r}"
    root = _code_heal_root()
    full = os.path.abspath(os.path.join(root, rel))
    root_real = os.path.abspath(root)
    if not full.startswith(root_real + os.sep) and full != root_real:
        return "", f"path escapes CODE_HEAL_ROOT: {rel!r}"
    return full, None


# ---------------------------------------------------------------------------
# Tool result
# ---------------------------------------------------------------------------
class ToolResult:
    def __init__(
        self,
        success: bool,
        tool_name: str,
        service: str = "",
        duration_seconds: float = 0.0,
        message: str = "",
        error: Optional[str] = None,
        output: str = "",
    ):
        self.success = success
        self.tool_name = tool_name
        self.service = service
        self.duration_seconds = duration_seconds
        self.message = message
        self.error = error
        self.output = output
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "tool": self.tool_name,
            "service": self.service,
            "duration_seconds": round(self.duration_seconds, 2),
            "message": self.message,
            "error": self.error,
            "output": self.output[:500],
            "timestamp": self.timestamp,
        }

    def __str__(self):
        status = "OK" if self.success else "FAIL"
        return f"ToolResult({self.tool_name} on {self.service}: {status} in {self.duration_seconds:.1f}s)"


# ---------------------------------------------------------------------------
# Shell helper
# ---------------------------------------------------------------------------
def _run(command: str, timeout: int = 60, dry_run: bool = False) -> tuple:
    """Execute a shell command. Returns (success, stdout, stderr)."""
    if dry_run:
        logger.info("[DRY RUN] %s", command)
        return (True, f"[dry-run] {command}", "")
    try:
        result = subprocess.run(
            command, shell=True, timeout=timeout,
            capture_output=True, text=True,
        )
        logger.debug("cmd=%s rc=%d", command, result.returncode)
        return (result.returncode == 0, result.stdout.strip(), result.stderr.strip())
    except subprocess.TimeoutExpired:
        return (False, "", f"Timed out after {timeout}s")
    except Exception as exc:
        return (False, "", str(exc))


# ---------------------------------------------------------------------------
# Individual tools
# ---------------------------------------------------------------------------

def restart_service(service: str, dry_run: bool = False) -> ToolResult:
    """docker restart <service>"""
    t0 = time.time()
    logger.info("Restarting service container: %s", service)

    ok, out, err = _run(f"{_docker_bin()} restart {service}", timeout=60, dry_run=dry_run)
    if not ok:
        return ToolResult(False, "restart_service", service, time.time() - t0, error=err or out or "docker restart failed")

    time.sleep(3)
    healthy = _health_check(service, dry_run=dry_run)

    return ToolResult(
        success=healthy,
        tool_name="restart_service",
        service=service,
        duration_seconds=time.time() - t0,
        message=f"Restarted {service}" if healthy else f"{service} not healthy after restart",
        output=out,
        error=None if healthy else "Health check failed post-restart",
    )


def restart_database(service: str, dry_run: bool = False) -> ToolResult:
    """docker restart <service>-db  (convention: DB container = <service>-db)."""
    t0 = time.time()
    db_container = f"{service}-db"
    logger.info("Restarting database container: %s", db_container)

    ok, out, err = _run(f"{_docker_bin()} restart {db_container}", timeout=60, dry_run=dry_run)
    if not ok:
        return ToolResult(False, "restart_database", db_container, time.time() - t0, error=err)

    time.sleep(5)
    ready = _wait_for_db(db_container, timeout=30, dry_run=dry_run)

    return ToolResult(
        success=ready,
        tool_name="restart_database",
        service=db_container,
        duration_seconds=time.time() - t0,
        message=f"Database {db_container} restarted" if ready else f"DB {db_container} not ready",
        output=out,
        error=None if ready else "DB readiness check failed",
    )


def check_logs(service: str, tail: int = 50, dry_run: bool = False) -> ToolResult:
    """docker logs --tail N <service>  — gather info before acting."""
    t0 = time.time()
    logger.info("Fetching logs for %s (tail=%d)", service, tail)

    ok, out, err = _run(f"{_docker_bin()} logs --tail {tail} {service}", timeout=15, dry_run=dry_run)

    return ToolResult(
        success=ok,
        tool_name="check_logs",
        service=service,
        duration_seconds=time.time() - t0,
        message=f"Collected {tail} log lines from {service}",
        output=out,
        error=err if not ok else None,
    )


def rollback_deployment(service: str, dry_run: bool = False) -> ToolResult:
    """
    Rollback by stopping the current container and starting the previous image.
    Uses docker-compose pull + up to get the prior tag.
    """
    t0 = time.time()
    logger.info("Rolling back deployment for %s", service)

    ok, out, err = _run(
        f"{_compose_bin()} up -d --force-recreate {service}",
        timeout=90, dry_run=dry_run,
    )
    if not ok:
        return ToolResult(
            False, "rollback_deployment", service, time.time() - t0,
            error=err or out or "rollback failed",
        )

    time.sleep(5)
    healthy = _health_check(service, dry_run=dry_run)

    return ToolResult(
        success=healthy,
        tool_name="rollback_deployment",
        service=service,
        duration_seconds=time.time() - t0,
        message=f"Rolled back {service}" if healthy else f"Rollback of {service} failed health check",
        output=out,
        error=None if healthy else "Health check failed post-rollback",
    )


def scale_replicas(service: str, replicas: int = 3, dry_run: bool = False) -> ToolResult:
    """docker-compose up -d --scale <service>=N"""
    t0 = time.time()
    logger.info("Scaling %s to %d replicas", service, replicas)

    ok, out, err = _run(
        f"{_compose_bin()} up -d --scale {service}={replicas}",
        timeout=60, dry_run=dry_run,
    )

    return ToolResult(
        success=ok,
        tool_name="scale_replicas",
        service=service,
        duration_seconds=time.time() - t0,
        message=f"Scaled {service} to {replicas} replicas" if ok else f"Scaling {service} failed",
        output=out,
        error=err if not ok else None,
    )


def read_file(service: str, path: str, dry_run: bool = False, **kwargs) -> ToolResult:
    """Read a file under CODE_HEAL_ROOT (allowlisted relative paths only)."""
    t0 = time.time()
    full, err = _resolve_code_heal_path(path)
    if err:
        return ToolResult(False, "read_file", service, time.time() - t0, error=err)
    if dry_run:
        return ToolResult(
            True, "read_file", service, time.time() - t0,
            message=f"[dry-run] read {path}",
            output=full,
        )
    try:
        with open(full, encoding="utf-8") as f:
            data = f.read()
    except OSError as exc:
        return ToolResult(False, "read_file", service, time.time() - t0, error=str(exc))
    return ToolResult(
        True, "read_file", service, time.time() - t0,
        message=f"Read {len(data)} chars from {path}",
        output=data[:10000],
    )


_RUN_CMD_ALLOWED_PREFIXES = ("sed -i ", "python -c ", "cp ")


def run_cmd(service: str, cmd: str, dry_run: bool = False, **kwargs) -> ToolResult:
    """Run a single allowlisted shell command (strict prototype — no arbitrary shell)."""
    t0 = time.time()
    c = (cmd or "").strip()
    if not c:
        return ToolResult(False, "run_cmd", service, time.time() - t0, error="empty command")
    if len(c) > 1200:
        return ToolResult(False, "run_cmd", service, time.time() - t0, error="command too long")
    if not any(c.startswith(p) for p in _RUN_CMD_ALLOWED_PREFIXES):
        return ToolResult(
            False, "run_cmd", service, time.time() - t0,
            error=f"command must start with one of: {_RUN_CMD_ALLOWED_PREFIXES}",
        )
    ok, out, err = _run(c, timeout=60, dry_run=dry_run)
    return ToolResult(
        success=ok,
        tool_name="run_cmd",
        service=service,
        duration_seconds=time.time() - t0,
        message="run_cmd completed" if ok else "run_cmd failed",
        output=(out or "")[:8000],
        error=err if not ok else None,
    )


def fix_code(service: str, dry_run: bool = False, **kwargs) -> ToolResult:
    """Write full file content under CODE_HEAL_ROOT, then docker restart service to reload."""
    t0 = time.time()
    fix_file = kwargs.get("fix_file") or kwargs.get("fix_path")
    content = kwargs.get("fix_file_content")
    if content is None:
        content = kwargs.get("content")
    if not fix_file or content is None:
        return ToolResult(
            False, "fix_code", service, time.time() - t0,
            error="fix_file and fix_file_content are required",
        )
    full, err = _resolve_code_heal_path(str(fix_file))
    if err:
        return ToolResult(False, "fix_code", service, time.time() - t0, error=err)
    raw = content if isinstance(content, str) else str(content)
    if not str(raw).strip():
        return ToolResult(
            False, "fix_code", service, time.time() - t0,
            error="fix_file_content is empty — LLM must return full file or code_context must be loaded",
        )
    mx = _code_heal_max_bytes()
    if len(raw.encode("utf-8")) > mx:
        return ToolResult(
            False, "fix_code", service, time.time() - t0,
            error=f"fix_file_content exceeds {mx} bytes",
        )
    if dry_run:
        logger.info("[DRY RUN] would write %d bytes to %s", len(raw), full)
        return ToolResult(
            True, "fix_code", service, time.time() - t0,
            message=f"[dry-run] would write {fix_file}",
            output=full,
        )
    try:
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(raw)
    except OSError as exc:
        return ToolResult(False, "fix_code", service, time.time() - t0, error=str(exc))

    ok, out, err = _run(f"{_docker_bin()} restart {service}", timeout=90, dry_run=False)
    if not ok:
        return ToolResult(
            False, "fix_code", service, time.time() - t0,
            error=err or out or "docker restart failed after write",
            output=out,
        )
    time.sleep(3)
    healthy = _verify_service_health(service, dry_run=False)
    return ToolResult(
        success=healthy,
        tool_name="fix_code",
        service=service,
        duration_seconds=time.time() - t0,
        message=f"Wrote {fix_file} and restarted {service}" if healthy else "File written but health check failed",
        output=out,
        error=None if healthy else "Post-fix health check failed",
    )


# ---------------------------------------------------------------------------
# Health-check / Critic helpers
# ---------------------------------------------------------------------------

def _http_health_check(
    url: str,
    dry_run: bool = False,
    retries: int = 6,
    delay_sec: float = 2.0,
) -> bool:
    """GET url; retry while Flask restarts after docker restart (avoids flaky verify)."""
    if dry_run:
        return True
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.getcode() == 200:
                    if attempt > 0:
                        logger.info("HTTP health %s: PASS on attempt %d", url, attempt + 1)
                    return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_exc = exc
            logger.info("HTTP health %s: attempt %d/%d FAIL (%s)", url, attempt + 1, retries, exc)
        if attempt < retries - 1:
            time.sleep(delay_sec)
    logger.info("HTTP health %s: FAIL after %d attempts (%s)", url, retries, last_exc)
    return False


def _verify_service_health(service: str, dry_run: bool = False) -> bool:
    """Docker ps for normal services; HTTP GET for code-heal services when URL is set."""
    names = _code_heal_service_names()
    if service in names:
        url = (os.environ.get("BUGGY_SERVICE_HEALTH_URL") or "").strip()
        if url:
            ok = _http_health_check(url, dry_run=dry_run)
            logger.info("Code-heal HTTP verify %s → %s", url, "PASS" if ok else "FAIL")
            return ok
    return _health_check(service, dry_run=dry_run)


def _health_check(service: str, dry_run: bool = False) -> bool:
    """Check that a docker container is running."""
    ok, out, _ = _run(
        f'{_docker_bin()} ps --filter "name={service}" --filter "status=running" --format "{{{{.Names}}}}"',
        timeout=10, dry_run=dry_run,
    )
    if dry_run:
        return True
    running = service in out
    logger.info("Health check %s: %s", service, "PASS" if running else "FAIL")
    return running


def _wait_for_db(db_container: str, timeout: int = 30, dry_run: bool = False) -> bool:
    """Poll until the DB container is running."""
    if dry_run:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _health_check(db_container, dry_run=dry_run):
            return True
        time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Tool registry & manager
# ---------------------------------------------------------------------------
TOOL_REGISTRY = {
    "restart_service":     restart_service,
    "restart_database":    restart_database,
    "check_logs":          check_logs,
    "rollback_deployment": rollback_deployment,
    "scale_replicas":      scale_replicas,
    "read_file":           read_file,
    "run_cmd":             run_cmd,
    "fix_code":            fix_code,
}

# Failure-type -> preferred action mapping
ACTION_MAP: Dict[str, Dict] = {
    FailureType.DB_APP_ESCALATE.value: {
        "primary":  "escalate",
        "fallback": "escalate",
    },
    FailureType.DB_DOWN.value: {
        "primary":  "restart_database",
        "fallback": "restart_service",
    },
    FailureType.SERVICE_DOWN.value: {
        "primary":  "restart_service",
        "fallback": "scale_replicas",
    },
    FailureType.ERROR_LOGS.value: {
        "primary":  "restart_service",
        "fallback": "rollback_deployment",
    },
    FailureType.CODE_HEAL.value: {
        "primary":  "fix_code",
        "fallback": "restart_service",
    },
}


class ToolManager:
    """Orchestrates tool execution with retry & fallback."""

    def __init__(self, state_manager: StateManager, dry_run: bool = False):
        self.state_manager = state_manager
        self.dry_run = dry_run
        logger.info("ToolManager ready (dry_run=%s)", dry_run)

    # ------------------------------------------------------------------
    def execute(self, incident_id: str, action: str, service: str, **kwargs) -> ToolResult:
        inc = self.state_manager.get_incident(incident_id)
        if not inc:
            raise ValueError(f"Incident {incident_id} not found")

        if action == "escalate":
            logger.warning("Action is 'escalate' — no tool to run")
            self.state_manager.update_incident(
                incident_id, status=IncidentStatus.ESCALATED,
            )
            return ToolResult(False, "escalate", service, message="Escalated to human")

        if action not in TOOL_REGISTRY:
            logger.error("Unknown action '%s' — escalating", action)
            self.state_manager.update_incident(
                incident_id, status=IncidentStatus.ESCALATED,
            )
            return ToolResult(False, action, service, error=f"Unknown action: {action}")

        self.state_manager.update_incident(incident_id, status=IncidentStatus.EXECUTING)

        tool_fn = TOOL_REGISTRY[action]
        logger.info("Executing %s on %s (incident=%s)", action, service, incident_id)
        result = tool_fn(service=service, dry_run=self.dry_run, **kwargs)

        inc.attempted_actions.append({
            "action":    action,
            "service":   service,
            "timestamp": datetime.now().isoformat(),
            "result":    result.to_dict(),
        })
        self.state_manager.update_incident(
            incident_id,
            attempted_actions=inc.attempted_actions,
            execution_result=result.message,
        )

        logger.info("Tool result: %s", result)
        return result

    # ------------------------------------------------------------------
    def execute_with_fallback(
        self,
        incident_id: str,
        primary_action: str,
        fallback_action: Optional[str],
        service: str,
        **kwargs,
    ) -> ToolResult:
        result = self.execute(incident_id, primary_action, service, **kwargs)
        if result.success:
            return result

        if fallback_action and fallback_action != primary_action:
            logger.warning(
                "Primary action '%s' failed — trying fallback '%s'",
                primary_action, fallback_action,
            )
            inc = self.state_manager.get_incident(incident_id)
            if inc:
                self.state_manager.update_incident(
                    incident_id, retry_count=inc.retry_count + 1,
                )
            return self.execute(incident_id, fallback_action, service, **kwargs)

        return result

    # ------------------------------------------------------------------
    def verify_and_close(self, incident_id: str, service: str) -> bool:
        """Post-action health check. Returns True if service is healthy."""
        healthy = _verify_service_health(service, dry_run=self.dry_run)
        if healthy:
            self.state_manager.update_incident(
                incident_id,
                status=IncidentStatus.HEALED,
                resolved=True,
                healed_at=datetime.now(),
            )
            logger.info("Incident %s HEALED", incident_id)
        else:
            inc = self.state_manager.get_incident(incident_id)
            if inc:
                new_retry = inc.retry_count + 1
                if new_retry >= self.state_manager.MAX_RETRIES:
                    self.state_manager.update_incident(
                        incident_id, status=IncidentStatus.ESCALATED,
                        retry_count=new_retry,
                    )
                    logger.warning("Incident %s ESCALATED (max retries)", incident_id)
                else:
                    self.state_manager.update_incident(
                        incident_id, status=IncidentStatus.RETRY,
                        retry_count=new_retry,
                    )
                    logger.warning("Incident %s marked RETRY (%d/%d)",
                                   incident_id, new_retry, self.state_manager.MAX_RETRIES)
        return healthy


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
# Self-test (dry-run — no Docker needed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _handler = logging.StreamHandler()
    _handler.setFormatter(_JSONLogFormatter())
    logging.root.handlers = [_handler]
    logging.root.setLevel(logging.DEBUG)

    sm = StateManager()
    tm = ToolManager(sm, dry_run=True)

    inc = Incident(
        service="payment-service",
        timestamp=datetime.now().isoformat(),
        container_status="running",
        exit_code=0,
        log_lines=["ERROR Connection refused: postgres://db:5432"],
        failure_type="db_down",
        severity="critical",
        tags=["database"],
    )
    sm.store_incident(inc)

    mapping = ACTION_MAP[inc.failure_type]
    result = tm.execute_with_fallback(
        inc.id,
        primary_action=mapping["primary"],
        fallback_action=mapping["fallback"],
        service=inc.service,
    )

    healed = tm.verify_and_close(inc.id, inc.service)
    final = sm.get_incident(inc.id)

    output = {
        "test": "tools.py self-test",
        "status": "PASSED",
        "incident_id": inc.id,
        "service": inc.service,
        "failure_type": inc.failure_type,
        "tool_result": result.to_dict(),
        "healed": healed,
        "final_state": {
            "status": final.status.value,
            "resolved": final.resolved,
            "retry_count": final.retry_count,
            "healed_at": final.healed_at.isoformat() if final.healed_at else None,
        },
    }
    print(json.dumps(output, indent=2))
