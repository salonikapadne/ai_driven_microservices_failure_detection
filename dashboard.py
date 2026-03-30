# dashboard.py — Socket.IO bridge: feeds ai_engine output to the React frontend
#
# Integration only — no AI logic lives here.
# Serves Socket.IO on port 4000 for the React app. Legacy Node `ai-service/` is not
# used by this stack. Optional `mail-service/` (Resend or Gmail) sends mail when MAILER_URL is set.
#
# Events emitted to frontend:
#   initial_state     {logs, services, rcaEvents, alertEmails, envAlertEmails, aiEngineLogs}  on connect
#   new_log           raw log object from log-collector          per RabbitMQ message
#   status_update     {service: {status, exitCode, lastSeen}}   per RabbitMQ message
#   rca_event         {service, rca, command, timestamp, alert_kind?}  after analysis or on analysis exception
#   ai_engine_log     {timestamp, logger, level, message, exc_info?}  from ai_engine loggers
#   email_list_update {emails: [...]}                            after add/remove
#
# Events received from frontend:
#   add_email    {email: "user@example.com"}
#   remove_email {email: "user@example.com"}

import html
import json
import logging
import traceback
from collections import deque
import os
import smtplib
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()

from flask import Flask
from flask_socketio import SocketIO

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _rabbitmq_url() -> str:
    if os.getenv("RABBITMQ_URL"):
        return os.getenv("RABBITMQ_URL")
    host = os.getenv("RABBITMQ_HOST", "rabbitmq")
    port = os.getenv("RABBITMQ_PORT", "5672")
    user = os.getenv("RABBITMQ_USER", "guest")
    pwd  = os.getenv("RABBITMQ_PASS", "guest")
    return f"amqp://{user}:{pwd}@{host}:{port}/"


RABBITMQ_URL     = _rabbitmq_url()
RABBITMQ_QUEUE   = os.getenv("RABBITMQ_QUEUE", "logs_queue")
DRY_RUN          = (os.getenv("DRY_RUN") or os.getenv("TOOLS_DRY_RUN", "false")).lower() == "true"
DASHBOARD_HOST   = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT   = int(os.getenv("DASHBOARD_PORT", "4000"))
COOLDOWN_SECONDS = int(os.getenv("ANALYSIS_COOLDOWN", "45"))
RETRY_DELAY      = 5
RETRY_DELAY_MAX  = 30

# Email — sender credentials read from env; recipients = env list + dashboard UI list
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")
# Optional nodemailer sidecar (see mail-service/): when set, dashboard POSTs instead of smtplib
MAILER_URL = (os.getenv("MAILER_URL") or "").strip()
MAILER_INTERNAL_TOKEN = (os.getenv("MAILER_INTERNAL_TOKEN") or "").strip()


def _parse_env_alert_recipients(raw: str | None) -> list:
    """Comma-separated emails from EMAIL_ALERT_RECIPIENTS; order preserved, deduped."""
    if not raw:
        return []
    out: list = []
    for part in raw.split(","):
        e = part.strip().lower()
        if e and "@" in e:
            out.append(e)
    return list(dict.fromkeys(out))


EMAIL_ALERT_RECIPIENTS = _parse_env_alert_recipients(os.getenv("EMAIL_ALERT_RECIPIENTS", ""))


def _all_alert_recipients() -> list:
    """Union of EMAIL_ALERT_RECIPIENTS and Socket.IO dashboard list (env first, deduped)."""
    with _emails_lock:
        ui = list(_alert_emails)
    return list(dict.fromkeys([*EMAIL_ALERT_RECIPIENTS, *ui]))

# Stream ai_engine (agent/state/tools) log records to Socket.IO + initial_state buffer
AI_ENGINE_LOG_TO_UI = os.getenv("AI_ENGINE_LOG_TO_UI", "true").lower() == "true"
AI_ENGINE_LOG_LEVEL = getattr(
    logging, (os.getenv("LOG_LEVEL") or "INFO").upper(), logging.INFO
)

MAX_LOGS = 1000
MAX_RCA  = 50
MAX_AI_ENGINE_LOGS = int(os.getenv("MAX_AI_ENGINE_LOGS", "300"))

_FAILURE_KEYWORDS = [
    "error", "exception", "traceback", "critical", "fatal",
    "connection refused", "oom killer", "out of memory",
    "segmentation fault", "timeout", "crashed", "killed",
]

# Beyond _FAILURE_KEYWORDS: app- and framework-specific signals that classify as failures
# but do not contain words like "error". Align [code_heal] with ai_engine.state.CODE_HEAL_MARKERS.
# Werkzeug dev access lines end with: ... HTTP/1.1" 500 -
_ANALYSIS_EXTRA_MARKERS = (
    "[code_heal]",
    " 500 -",
)

_ACTION_COMMANDS = {
    "restart_service":     "docker restart {service}",
    "restart_database":    "docker restart {service}-db",
    "rollback_deployment": "docker-compose up -d --force-recreate {service}",
    "scale_replicas":      "docker-compose up -d --scale {service}=3",
    "check_logs":          "docker logs --tail 50 {service}",
    "fix_code":            "write allowlisted file under CODE_HEAL_ROOT + docker restart {service}",
    "escalate":            "# Manual intervention required for {service}",
}

# ---------------------------------------------------------------------------
# Shared in-memory state
# ---------------------------------------------------------------------------
_state = {
    "logs":      [],
    "rcaEvents": [],
    "services": {
        "user-service":    {"status": "unknown", "exitCode": None, "lastSeen": None},
        "order-service":   {"status": "unknown", "exitCode": None, "lastSeen": None},
        "payment-service": {"status": "unknown", "exitCode": None, "lastSeen": None},
        "gateway-service": {"status": "unknown", "exitCode": None, "lastSeen": None},
        "hil-db-demo":      {"status": "unknown", "exitCode": None, "lastSeen": None},
        "buggy-service":    {"status": "unknown", "exitCode": None, "lastSeen": None},
    },
}
_state_lock = threading.Lock()

# Dynamic alert email list — managed via Socket.IO events from the frontend
_alert_emails: list = []
_emails_lock = threading.Lock()

# Per-service cooldown
_cooldown: dict = {}

# Ring buffer of recent ai_engine log lines (newest at index 0) for UI + reconnect
_ai_engine_logs: deque = deque(maxlen=MAX_AI_ENGINE_LOGS)
_ai_engine_logs_lock = threading.Lock()
_ai_engine_log_handler_installed = False
_ai_engine_log_handler_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Flask-SocketIO server
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = "microservices-monitor-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


def _emit_to_all_clients(event: str, data) -> None:
    """Emit to every connected Socket.IO client.

    RabbitMQ and analysis run in background threads. Wrapping in the Flask
    application context and default namespace avoids dropped or missing
    deliveries; note that ``broadcast=True`` is not supported on
    ``SocketIO.emit`` in flask-socketio 5.x (it is not forwarded to
    python-socketio and raises ``TypeError``).
    """
    with app.app_context():
        socketio.emit(event, data, namespace="/")


class _SocketIOAiEngineLogHandler(logging.Handler):
    """Forwards agent/state/tools log records to all Socket.IO clients."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "timestamp": datetime.fromtimestamp(record.created).isoformat(),
                "logger":    record.name,
                "level":     record.levelname,
                "message":   record.getMessage(),
            }
            if record.exc_info:
                payload["exc_info"] = self.formatter.formatException(record.exc_info)
            with _ai_engine_logs_lock:
                _ai_engine_logs.appendleft(payload)
            _emit_to_all_clients("ai_engine_log", payload)
        except Exception:
            logging.getLogger("dashboard").debug("ai_engine log bridge emit failed", exc_info=True)


def _install_ai_engine_log_handler() -> None:
    """Attach bridge handler to ai_engine loggers once (lazy, when agent first loads)."""
    global _ai_engine_log_handler_installed
    if not AI_ENGINE_LOG_TO_UI:
        return
    with _ai_engine_log_handler_lock:
        if _ai_engine_log_handler_installed:
            return
        h = _SocketIOAiEngineLogHandler()
        h.setLevel(AI_ENGINE_LOG_LEVEL)
        h.setFormatter(logging.Formatter())
        for name in ("agent", "state", "tools"):
            log = logging.getLogger(name)
            log.setLevel(AI_ENGINE_LOG_LEVEL)
            log.addHandler(h)
        _ai_engine_log_handler_installed = True
        logger.info("ai_engine log bridge installed (loggers=agent,state,tools level=%s)",
                    logging.getLevelName(AI_ENGINE_LOG_LEVEL))


@socketio.on("connect")
def _on_connect():
    with _state_lock:
        snapshot = {
            "logs":      list(_state["logs"]),
            "services":  dict(_state["services"]),
            "rcaEvents": list(_state["rcaEvents"]),
        }
    with _emails_lock:
        snapshot["alertEmails"] = list(_alert_emails)
    snapshot["envAlertEmails"] = list(EMAIL_ALERT_RECIPIENTS)
    with _ai_engine_logs_lock:
        snapshot["aiEngineLogs"] = list(_ai_engine_logs)

    socketio.emit("initial_state", snapshot, namespace="/")
    logger.info("Frontend connected — sent initial_state (%d logs, %d rca, %d emails, %d ai_engine)",
                len(snapshot["logs"]), len(snapshot["rcaEvents"]),
                len(snapshot["alertEmails"]), len(snapshot["aiEngineLogs"]))


@socketio.on("add_email")
def _on_add_email(data):
    """Add an email address to the alert list."""
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        socketio.emit("email_error", {"message": f"Invalid email address: {email}"}, namespace="/")
        return

    with _emails_lock:
        if email in _alert_emails:
            socketio.emit("email_error", {"message": f"{email} is already in the list"}, namespace="/")
            return
        _alert_emails.append(email)
        emails = list(_alert_emails)

    socketio.emit("email_list_update", {"emails": emails}, namespace="/")
    logger.info("Alert email added: %s (total: %d)", email, len(emails))


@socketio.on("remove_email")
def _on_remove_email(data):
    """Remove an email address from the alert list."""
    email = (data.get("email") or "").strip().lower()

    with _emails_lock:
        if email in _alert_emails:
            _alert_emails.remove(email)
        emails = list(_alert_emails)

    socketio.emit("email_list_update", {"emails": emails}, namespace="/")
    logger.info("Alert email removed: %s (total: %d)", email, len(emails))


# ---------------------------------------------------------------------------
# Email alerting
# ---------------------------------------------------------------------------
def _send_email_via_mailer(recipients: list, rca_event: dict) -> None:
    """POST to mail-service (nodemailer) with shared token."""
    if not MAILER_INTERNAL_TOKEN:
        logger.warning("MAILER_URL is set but MAILER_INTERNAL_TOKEN is empty — skipping email")
        return
    url = MAILER_URL.rstrip("/") + "/send-alert"
    payload = json.dumps({"recipients": recipients, "rca_event": rca_event}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Internal-Token": MAILER_INTERNAL_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            code = getattr(resp, "status", None) or resp.getcode()
            if code != 200:
                logger.error("mail-service returned HTTP %s", code)
                return
        logger.info("Email alert sent via mail-service to: %s", ", ".join(recipients))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        logger.error("mail-service HTTP %s: %s", exc.code, err_body[:500])
    except Exception as exc:
        logger.error("mail-service request failed: %s", exc)


def _send_email_smtp(recipients: list, rca_event: dict) -> None:
    """Send using Gmail SMTP in-process (used when MAILER_URL is unset)."""
    service   = rca_event.get("service", "unknown")
    rca       = rca_event.get("rca", "")
    command   = rca_event.get("command", "")
    timestamp = rca_event.get("timestamp", datetime.now().isoformat())
    kind      = rca_event.get("alert_kind", "failure")

    esc_svc = html.escape(service, quote=True)
    esc_cmd = html.escape(command, quote=True)
    esc_rca = html.escape(rca, quote=True)

    if kind == "analysis_error":
        title_plain = f"Analysis failed for {service}"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"\U0001f6a8 Alert: {title_plain}"
        msg["From"]    = EMAIL_USER
        msg["To"]      = ", ".join(recipients)
        text_body = (
            f"AI analysis error: {service}\n\n"
            f"Details:\n{rca}\n\n"
            f"Command line (not executed):\n{command}\n\n"
            f"Time: {timestamp}"
        )
        html_body = f"""
    <div style="font-family: Inter, system-ui, sans-serif; max-width: 600px; margin: auto;">
      <div style="background: #92400e; color: white; padding: 20px 24px; border-radius: 8px 8px 0 0;">
        <h2 style="margin:0;">\U0001f6a8 AI analysis failed: {esc_svc}</h2>
      </div>
      <div style="background: #f9fafb; padding: 24px; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 8px 8px;">
        <h3 style="color: #111827; margin-bottom: 8px;">Details</h3>
        <pre style="background: #1f2937; color: #fde68a; padding: 12px 16px; border-radius: 6px;
                    font-size: 12px; overflow-x: auto; white-space: pre-wrap;">{esc_rca}</pre>

        <h3 style="color: #111827; margin-top: 20px; margin-bottom: 8px;">Healing command</h3>
        <pre style="background: #1f2937; color: #a7f3d0; padding: 12px 16px; border-radius: 6px;
                    font-size: 13px; overflow-x: auto;">{esc_cmd}</pre>

        <p style="margin-top: 20px; font-size: 12px; color: #9ca3af;">
          Time: {html.escape(timestamp, quote=True)}
        </p>
      </div>
    </div>
    """
    elif kind == "escalation":
        title_plain = f"Human escalation required for {service}"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"\U0001f6a8 {title_plain}"
        msg["From"]    = EMAIL_USER
        msg["To"]      = ", ".join(recipients)
        text_body = (
            f"Human escalation: {service}\n\n"
            f"No automated Docker command was executed. Review the details below.\n\n"
            f"Details:\n{rca}\n\n"
            f"Suggested manual follow-up (placeholder):\n{command}\n\n"
            f"Time: {timestamp}"
        )
        html_body = f"""
    <div style="font-family: Inter, system-ui, sans-serif; max-width: 600px; margin: auto;">
      <div style="background: #7c2d12; color: white; padding: 20px 24px; border-radius: 8px 8px 0 0;">
        <h2 style="margin:0;">\U0001f6a8 Human escalation: {esc_svc}</h2>
      </div>
      <div style="background: #f9fafb; padding: 24px; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 8px 8px;">
        <p style="color: #374151; margin-bottom: 16px;">No automated container action was run. An engineer should investigate.</p>
        <h3 style="color: #111827; margin-bottom: 8px;">Context</h3>
        <p style="color: #374151; line-height: 1.6; white-space: pre-wrap;">{esc_rca}</p>
        <h3 style="color: #111827; margin-top: 20px; margin-bottom: 8px;">Manual follow-up</h3>
        <pre style="background: #1f2937; color: #fde68a; padding: 12px 16px; border-radius: 6px;
                    font-size: 13px; overflow-x: auto; white-space: pre-wrap;">{esc_cmd}</pre>
        <p style="margin-top: 20px; font-size: 12px; color: #9ca3af;">{html.escape(timestamp, quote=True)}</p>
      </div>
    </div>
    """
    else:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"\U0001f6a8 Alert: Failure detected in {service}"
        msg["From"]    = EMAIL_USER
        msg["To"]      = ", ".join(recipients)

        text_body = (
            f"Microservice Alert: {service}\n\n"
            f"Root Cause Analysis:\n{rca}\n\n"
            f"Healing Command Executed:\n{command}\n\n"
            f"Time: {timestamp}"
        )

        html_body = f"""
    <div style="font-family: Inter, system-ui, sans-serif; max-width: 600px; margin: auto;">
      <div style="background: #1e5631; color: white; padding: 20px 24px; border-radius: 8px 8px 0 0;">
        <h2 style="margin:0;">\U0001f6a8 Microservice Alert: {esc_svc}</h2>
      </div>
      <div style="background: #f9fafb; padding: 24px; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 8px 8px;">
        <h3 style="color: #111827; margin-bottom: 8px;">Root Cause Analysis</h3>
        <p style="color: #374151; line-height: 1.6; white-space: pre-wrap;">{esc_rca}</p>

        <h3 style="color: #111827; margin-top: 20px; margin-bottom: 8px;">Healing Command Executed</h3>
        <pre style="background: #1f2937; color: #a7f3d0; padding: 12px 16px; border-radius: 6px;
                    font-size: 13px; overflow-x: auto;">{esc_cmd}</pre>

        <p style="margin-top: 20px; font-size: 12px; color: #9ca3af;">
          Detected at: {html.escape(timestamp, quote=True)}
        </p>
      </div>
    </div>
    """

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, recipients, msg.as_string())
        logger.info("Email alert sent to: %s", ", ".join(recipients))
    except Exception as exc:
        logger.error("Email send failed: %s", exc)


def _send_email_alert(rca_event: dict) -> None:
    """Send an HTML email alert to env + dashboard recipients (mailer sidecar or SMTP)."""
    recipients = _all_alert_recipients()
    if not recipients:
        logger.warning(
            "Alert email skipped: no recipients (add emails in dashboard UI and/or EMAIL_ALERT_RECIPIENTS)",
        )
        return

    if MAILER_URL:
        _send_email_via_mailer(recipients, rca_event)
        return

    if not EMAIL_USER or not EMAIL_PASS:
        logger.warning("Alert email skipped: EMAIL_USER/EMAIL_PASS not set (MAILER_URL is empty)")
        return

    _send_email_smtp(recipients, rca_event)


# ---------------------------------------------------------------------------
# Agent singleton
# ---------------------------------------------------------------------------
_agent      = None
_agent_lock = threading.Lock()


def _get_agent():
    global _agent
    if _agent is None:
        with _agent_lock:
            if _agent is None:
                from ai_engine.state import StateManager
                from ai_engine.tools import ToolManager
                from ai_engine.agent import Agent

                _install_ai_engine_log_handler()

                provider  = os.getenv("LLM_PROVIDER", "vertex_ai")
                fallbacks = [
                    p.strip()
                    for p in os.getenv("LLM_FALLBACKS", "gemini,openai").split(",")
                    if p.strip()
                ]
                sm     = StateManager()
                tm     = ToolManager(sm, dry_run=DRY_RUN)
                _agent = Agent(sm, tm, llm_provider=provider, fallback_providers=fallbacks)
                logger.info(
                    "Agent ready (provider=%s, dry_run=%s)",
                    _agent.active_provider,
                    DRY_RUN,
                )
    return _agent


# ---------------------------------------------------------------------------
# Failure detection helpers
# ---------------------------------------------------------------------------
def _needs_analysis(logobj: dict) -> bool:
    exit_code = logobj.get("exit_code")
    lines = logobj.get("logs", [])
    if exit_code not in (0, None):
        return True
    matched_kw = None
    for line in lines:
        low = line.lower()
        for marker in _ANALYSIS_EXTRA_MARKERS:
            if marker in low:
                matched_kw = marker
                break
        if matched_kw:
            break
        for kw in _FAILURE_KEYWORDS:
            if kw in low:
                matched_kw = kw
                break
        if matched_kw:
            break
    return matched_kw is not None


def _in_cooldown(service: str) -> bool:
    return (time.time() - _cooldown.get(service, 0)) < COOLDOWN_SECONDS


def _set_cooldown(service: str):
    _cooldown[service] = time.time()


def _code_heal_service_names() -> set:
    raw = os.getenv("CODE_HEAL_SERVICES", "buggy-service")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _load_code_context(service: str) -> dict:
    """Read allowlisted files under CODE_HEAL_ROOT for the agent prompt (same paths ai-engine may write)."""
    if service not in _code_heal_service_names():
        return {}
    root = os.getenv("CODE_HEAL_ROOT", "/buggy-live")
    files = [f.strip() for f in os.getenv("CODE_HEAL_FILES", "app.py").split(",") if f.strip()]
    out = {}
    for rel in files:
        path = os.path.join(root, rel.replace("\\", "/").lstrip("/"))
        try:
            with open(path, encoding="utf-8") as fp:
                out[rel] = fp.read()
        except OSError:
            logger.debug("code_context: skip missing or unreadable %s", path)
    return out


# ---------------------------------------------------------------------------
# Analysis thread: calls agent.run(), emits rca_event, sends email
# ---------------------------------------------------------------------------
def _run_analysis(logobj: dict) -> None:
    service = logobj.get("service", "unknown")
    try:
        agent  = _get_agent()
        code_ctx = _load_code_context(service)
        result = agent.run(
            service=service,
            log_lines=logobj.get("logs", []),
            container_status=logobj.get("container_status", "unknown"),
            exit_code=int(logobj.get("exit_code") or 0),
            timestamp=logobj.get("timestamp"),
            code_context=code_ctx,
        )

        decision = result.get("decision", {})
        action   = decision.get("action", "escalate")
        target   = decision.get("service") or service

        cmd_tmpl = _ACTION_COMMANDS.get(action, "docker restart {service}")
        command  = cmd_tmpl.format(service=target)

        rca_text = " ".join(filter(None, [
            decision.get("error_summary", ""),
            decision.get("root_cause", ""),
        ])) or f"Failure detected on {target} — action: {action}"

        final_status = (result.get("final_status") or "").lower()
        is_escalation = final_status == "escalated" or action == "escalate"
        if is_escalation:
            rca_text = (
                "Escalated — human intervention required.\n\n" + rca_text.strip()
            )

        rca_event = {
            "service":   target,
            "rca":       rca_text,
            "command":   command,
            "timestamp": datetime.now().isoformat(),
        }
        if is_escalation:
            rca_event["alert_kind"] = "escalation"

        with _state_lock:
            _state["rcaEvents"].insert(0, rca_event)
            if len(_state["rcaEvents"]) > MAX_RCA:
                _state["rcaEvents"].pop()

        _emit_to_all_clients("rca_event", rca_event)
        logger.info("rca_event emitted: service=%s action=%s healed=%s",
                    target, action, result.get("healed"))

        # Send email alert to all registered recipients
        _send_email_alert(rca_event)

    except Exception as exc:
        logger.error("Analysis failed for %s: %s", service, exc, exc_info=True)
        tb = traceback.format_exc()
        if len(tb) > 6000:
            tb = tb[:6000] + "\n... (truncated)"
        detail = f"Exception: {exc!r}\n\nTraceback:\n{tb}"
        failure_event = {
            "service":   service,
            "rca":       detail,
            "command":   "# Analysis did not complete — no healing command was executed",
            "timestamp": datetime.now().isoformat(),
            "alert_kind": "analysis_error",
        }
        with _state_lock:
            _state["rcaEvents"].insert(0, failure_event)
            if len(_state["rcaEvents"]) > MAX_RCA:
                _state["rcaEvents"].pop()
        _emit_to_all_clients("rca_event", failure_event)
        _send_email_alert(failure_event)


# ---------------------------------------------------------------------------
# RabbitMQ message handler
# ---------------------------------------------------------------------------
def _on_message(channel, method, _properties, body) -> None:
    try:
        logobj = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Non-JSON message received — skipping")
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    service          = logobj.get("service", "unknown")
    container_status = logobj.get("container_status", "unknown")
    exit_code        = logobj.get("exit_code")
    log_lines        = logobj.get("logs", [])

    if not log_lines:
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    with _state_lock:
        _state["logs"].insert(0, logobj)
        if len(_state["logs"]) > MAX_LOGS:
            _state["logs"].pop()

        _state["services"][service] = {
            "status":   container_status,
            "exitCode": exit_code,
            "lastSeen": datetime.now().isoformat(),
        }

    _emit_to_all_clients("new_log", logobj)
    _emit_to_all_clients("status_update", dict(_state["services"]))

    if _needs_analysis(logobj) and not _in_cooldown(service):
        _set_cooldown(service)
        logger.info("Failure detected in %s — spawning analysis thread", service)
        t = threading.Thread(target=_run_analysis, args=(logobj,), daemon=True)
        t.start()

    channel.basic_ack(delivery_tag=method.delivery_tag)


# ---------------------------------------------------------------------------
# RabbitMQ consumer loop (background thread)
# ---------------------------------------------------------------------------
def _consume_loop() -> None:
    import pika

    delay = RETRY_DELAY
    while True:
        try:
            params = pika.URLParameters(RABBITMQ_URL)
            conn   = pika.BlockingConnection(params)
            ch     = conn.channel()
            # Passive: attach to existing queue without re-declaring (avoids PRECONDITION_FAILED
            # when another client created logs_queue with different durable flag).
            ch.queue_declare(queue=RABBITMQ_QUEUE, passive=True)
            ch.basic_qos(prefetch_count=1)
            ch.basic_consume(queue=RABBITMQ_QUEUE, on_message_callback=_on_message)
            logger.info("RabbitMQ connected — consuming '%s'", RABBITMQ_QUEUE)
            delay = RETRY_DELAY
            ch.start_consuming()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.error("RabbitMQ lost: %s — retry in %ds", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, RETRY_DELAY_MAX)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if MAILER_URL:
        email_status = f"mailer={MAILER_URL}"
    else:
        email_status = f"sender={EMAIL_USER}" if EMAIL_USER else "not configured (set MAILER_URL or EMAIL_USER)"
    logger.info(
        "Starting dashboard (port=%d, dry_run=%s, cooldown=%ds, email=%s)",
        DASHBOARD_PORT, DRY_RUN, COOLDOWN_SECONDS, email_status,
    )

    consumer_thread = threading.Thread(target=_consume_loop, daemon=True)
    consumer_thread.start()

    socketio.run(app, host=DASHBOARD_HOST, port=DASHBOARD_PORT, allow_unsafe_werkzeug=True)
