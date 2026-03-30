# ai_engine/agent.py — LangGraph-based incident response agent
#
# Graph topology:
#   classify → analyze → [execute | escalate] → verify → [END(healed) | analyze(retry) | escalate]
#
# Each node function is created via a factory that closes over the dependencies
# (state_manager, tool_manager, llm) so the graph remains a pure function graph.

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph

try:
    from .state import FailureType, Incident, IncidentStatus, StateManager, classify_failure
    from .tools import ACTION_MAP, ToolManager
except ImportError:
    from state import FailureType, Incident, IncidentStatus, StateManager, classify_failure
    from tools import ACTION_MAP, ToolManager

logger = logging.getLogger("agent")

ALLOWED_ACTIONS = [
    "restart_service",
    "restart_database",
    "check_logs",
    "rollback_deployment",
    "scale_replicas",
    "fix_code",
    "escalate",
]

# ---------------------------------------------------------------------------
# LangGraph state schema
# ---------------------------------------------------------------------------
class IncidentState(TypedDict):
    # Raw input
    service: str
    log_lines: List[str]
    container_status: str
    exit_code: int
    timestamp: str
    # Optional: live file snippets from dashboard (CODE_HEAL_ROOT / CODE_HEAL_FILES)
    code_context: Dict[str, str]
    # Populated by classify node
    incident_id: str
    failure_type: str
    error_keyword: str
    severity: str
    tags: List[str]
    # Populated by analyze node
    decision: Dict
    active_provider: str
    # Populated by execute node
    tool_result: Dict
    # Populated by verify / escalate nodes
    healed: bool
    retry_count: int
    final_status: str


# ---------------------------------------------------------------------------
# LLM client implementations
# ---------------------------------------------------------------------------
class VertexAIClient:
    """Google Cloud Vertex AI with Gemini models."""

    def __init__(self):
        try:
            import vertexai
            from vertexai.generative_models import GenerativeModel
        except ImportError:
            raise ImportError("Install: pip install google-cloud-aiplatform")

        self.project_id = os.getenv("VERTEX_AI_PROJECT_ID")
        self.location = os.getenv("VERTEX_AI_LOCATION", "us-central1")
        self.model_name = os.getenv("VERTEX_AI_MODEL", "gemini-2.0-flash")

        if not self.project_id:
            raise ValueError("VERTEX_AI_PROJECT_ID not set in environment")

        cred = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        if cred:
            if os.path.isdir(cred):
                raise ValueError(
                    f"GOOGLE_APPLICATION_CREDENTIALS is a directory ({cred!r}), not a JSON key file. "
                    "Fix the docker-compose volume so ./vertex-ai-key.json on the host is the real service account file."
                )
            if not os.path.isfile(cred):
                raise ValueError(f"GOOGLE_APPLICATION_CREDENTIALS file not found: {cred!r}")

        vertexai.init(project=self.project_id, location=self.location)
        self.model = GenerativeModel(self.model_name)
        logger.info("VertexAI ready (model=%s, region=%s)", self.model_name, self.location)

    def generate(self, prompt: str, temperature: float = 0.2) -> str:
        from vertexai.generative_models import GenerationConfig
        config = GenerationConfig(temperature=temperature, max_output_tokens=2048, top_p=0.95)
        response = self.model.generate_content([prompt], generation_config=config, stream=False)
        return response.text


class GeminiClient:
    """Direct Google Gemini API (google-generativeai SDK)."""

    def __init__(self):
        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError("Install: pip install google-generativeai")

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not set in environment")

        genai.configure(api_key=api_key)
        model_name = os.getenv("VERTEX_AI_MODEL", "gemini-2.0-flash")
        self.model = genai.GenerativeModel(model_name)
        logger.info("Gemini API ready (model=%s)", model_name)

    def generate(self, prompt: str, temperature: float = 0.2) -> str:
        response = self.model.generate_content(
            prompt,
            generation_config={"temperature": temperature, "max_output_tokens": 2048, "top_p": 0.95},
        )
        return response.text


class OpenAIClient:
    """OpenAI GPT-4 / GPT-3.5."""

    def __init__(self):
        try:
            import openai
        except ImportError:
            raise ImportError("Install: pip install openai")

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set in environment")

        self.client = openai.OpenAI(api_key=api_key)
        self.model = os.getenv("OPENAI_MODEL", "gpt-4")
        logger.info("OpenAI ready (model=%s)", self.model)

    def generate(self, prompt: str, temperature: float = 0.2) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": "You are a cloud infrastructure reliability engineer. Respond ONLY with raw JSON."},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content


class OllamaClient:
    """Local Ollama server (no API keys needed)."""

    def __init__(self):
        import urllib.request
        self.url = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
        self.model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")

        try:
            health_url = self.url.rsplit("/", 1)[0].rsplit("/", 1)[0]
            req = urllib.request.Request(health_url, method="GET")
            urllib.request.urlopen(req, timeout=5)
        except Exception as exc:
            raise ConnectionError(f"Ollama not reachable at {self.url}: {exc}")

        logger.info("Ollama ready (model=%s, url=%s)", self.model, self.url)

    def generate(self, prompt: str, temperature: float = 0.2) -> str:
        import urllib.request
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }).encode("utf-8")

        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body.get("response", "")


_PROVIDERS = {
    "vertex_ai": VertexAIClient,
    "gemini": GeminiClient,
    "openai": OpenAIClient,
    "local": OllamaClient,
}


# ---------------------------------------------------------------------------
# Prompt & response helpers
# ---------------------------------------------------------------------------
def _build_prompt(
    incident: Incident,
    state_manager: StateManager,
    code_context: Optional[Dict[str, str]] = None,
) -> str:
    past = [
        i for i in state_manager.get_service_incidents(incident.service)
        if i.resolved and i.healed_at
    ][-5:]
    if past:
        history_lines = []
        for i in past:
            dur = (i.healed_at - i.detected_at).total_seconds()
            history_lines.append(f"  - {i.failure_type}: fixed with '{i.suggested_action}' in {dur:.0f}s")
        history = "\n".join(history_lines)
    else:
        history = "  No historical data."

    steps_tried = json.dumps(incident.attempted_actions, indent=2) if incident.attempted_actions else "None"
    ctx = code_context or {}
    code_block = ""
    if ctx:
        parts = []
        for rel, body in ctx.items():
            snippet = body if len(body) <= 12000 else body[:12000] + "\n... [truncated]"
            parts.append(f"--- file: {rel} ---\n{snippet}")
        code_block = f"""
LIVE SOURCE (read-only context; fixes apply via fix_code to paths under CODE_HEAL_ROOT):
{chr(10).join(parts)}
"""

    is_code_heal = incident.failure_type == FailureType.CODE_HEAL.value

    if is_code_heal:
        return f"""You are an expert cloud infrastructure reliability engineer specialising in Docker-based microservices.

Analyse the following service failure. The failure is classified as code_heal: the logs indicate an application bug that can be fixed by editing the mounted live source file(s), then restarting the service.

Respond ONLY with raw JSON (no markdown, no backticks, no explanation outside JSON).

SERVICE CONTEXT:
  Service       : {incident.service}
  Container     : {incident.container_status}
  Exit Code     : {incident.exit_code}
  Failure Type  : {incident.failure_type}
  Severity      : {incident.severity}
  Detected At   : {incident.timestamp}
  Retry Count   : {incident.retry_count}

RECENT LOGS:
{chr(10).join(f'  - {line}' for line in incident.log_lines[-12:])}
{code_block}
TAGS: {', '.join(incident.tags)}
ERROR KEYWORD: {incident.error_keyword}

STEPS ALREADY TRIED (do NOT repeat these):
{steps_tried}

HISTORICAL CONTEXT:
{history}

ALLOWED ACTIONS (pick exactly one):
  fix_code            — provide full corrected file content for one allowlisted file; the engine writes under CODE_HEAL_ROOT and restarts the container
  restart_service     — only if a source fix is not possible
  escalate            — if retry_count >= 3 or fix is unsafe

RULES:
  - You MUST choose fix_code when the logs and source show a clear bug (e.g. wrong constant breaking /health).
  - fix_file must be a relative path allowlisted for this demo (e.g. app.py).
  - fix_file_content must be the ENTIRE file content after your fix, as a single JSON string (escape newlines as \\n).
  - If failure_type is code_heal and retry_count >= 3, you MUST choose escalate.
  - NEVER suggest an action that was already tried and failed.

REQUIRED JSON RESPONSE FORMAT (every field is mandatory; use empty strings for fix_* if action is not fix_code):
{{
  "action": "fix_code|restart_service|escalate",
  "service": "{incident.service}",
  "fix_file": "app.py",
  "fix_file_content": "<full file source when action is fix_code; empty string otherwise>",
  "error_summary": "<2-3 sentences>",
  "root_cause": "<2-3 sentences>",
  "fix_explanation": "<how the code change fixes the failure>",
  "reasoning": "<1-2 sentences>",
  "confidence": "high|medium|low",
  "alternative": "restart_service"
}}
"""

    return f"""You are an expert cloud infrastructure reliability engineer specialising in Docker-based microservices.

Analyse the following service failure incident. Your job is to:
  1. Identify WHAT the error is from the logs.
  2. Explain the ROOT CAUSE — why this failure likely occurred.
  3. Recommend ONE recovery action and explain HOW it fixes the problem.
  4. Justify WHY you chose this action over the alternatives.

Respond ONLY with raw JSON (no markdown, no backticks, no explanation outside JSON).

SERVICE CONTEXT:
  Service       : {incident.service}
  Container     : {incident.container_status}
  Exit Code     : {incident.exit_code}
  Failure Type  : {incident.failure_type}
  Severity      : {incident.severity}
  Detected At   : {incident.timestamp}
  Retry Count   : {incident.retry_count}

RECENT LOGS:
{chr(10).join(f'  - {line}' for line in incident.log_lines[-8:])}
{code_block}
TAGS: {', '.join(incident.tags)}
ERROR KEYWORD: {incident.error_keyword}

STEPS ALREADY TRIED (do NOT repeat these):
{steps_tried}

HISTORICAL CONTEXT:
{history}

ALLOWED ACTIONS (pick exactly one):
  restart_service     — docker restart the failing service container
  restart_database    — docker restart the database container
  check_logs          — docker logs to gather more info before acting
  rollback_deployment — rollback to previous container image
  scale_replicas      — increase replica count
  escalate            — flag for human intervention

RULES:
  - If failure_type is db_app_escalate, you MUST choose escalate — never restart_database or restart_service; this requires human intervention.
  - If failure_type is db_down, prefer restart_database first.
  - If failure_type is service_down, prefer restart_service.
  - If failure_type is service_down and Container is not running (exited, dead, stopped, or any non-running state), you MUST choose restart_service — do NOT escalate; a stopped container is healed by starting it again via restart.
  - If failure_type is error_logs and container is running, prefer restart_service or rollback.
  - If retry_count >= 3, you MUST choose escalate.
  - NEVER suggest an action that was already tried and failed.

REQUIRED JSON RESPONSE FORMAT (every field is mandatory):
{{
  "action": "<one of the allowed actions above>",
  "service": "<target service name>",
  "error_summary": "<2-3 sentences: what the error is, referencing specific log lines>",
  "root_cause": "<2-3 sentences: why this failure occurred — the underlying technical cause>",
  "fix_explanation": "<2-3 sentences: how the chosen Docker action resolves this specific failure>",
  "reasoning": "<1-2 sentences: why this action was chosen over the alternatives>",
  "confidence": "high|medium|low",
  "alternative": "<backup action if the primary fails>"
}}
"""


def _escalation_decision(incident: Incident) -> Dict:
    return {
        "action": "escalate",
        "service": incident.service,
        "error_summary": f"Incident on {incident.service} with failure type '{incident.failure_type}' could not be auto-resolved.",
        "root_cause": "Either the retry limit was reached after multiple failed recovery attempts, or the LLM provider was unavailable.",
        "fix_explanation": "This incident requires human intervention. An engineer should inspect container logs and apply a manual fix.",
        "reasoning": "Automatic recovery exhausted — escalating to on-call team.",
        "confidence": "high",
        "alternative": "escalate",
    }


def _db_app_hil_decision(incident: Incident) -> Dict:
    """Human-in-the-loop for application-level DB errors (classifier: db_app_escalate)."""
    return {
        "action": "escalate",
        "service": incident.service,
        "error_summary": (
            f"Application-level database error on {incident.service} (failure_type=db_app_escalate). "
            "Logs match a human-escalation pattern (e.g. migration failure); container restarts are not sufficient."
        ),
        "root_cause": (
            "The classifier marked this as an application/schema/credential-level database issue, "
            "not a simple connectivity outage."
        ),
        "fix_explanation": (
            "An engineer should inspect migrations, schema state, and credentials. "
            "Automated docker restart is intentionally not applied for this failure class."
        ),
        "reasoning": (
            "Policy: db_app_escalate always requires human-in-the-loop; no automated self-heal path."
        ),
        "confidence": "high",
        "alternative": "escalate",
    }


def _coerce_self_heal_decision(incident: Incident, decision: Dict, state_manager: StateManager) -> Dict:
    """If the model returned escalate but retries remain, run ACTION_MAP[failure_type] instead.

    The graph routes ``escalate`` to the escalate node (no Docker tools). Models often output
    escalate for db_down / error_logs or when JSON is malformed. While retries are not
    exhausted, map to the primary tool for the classified failure type.
    """
    if incident.failure_type == FailureType.DB_APP_ESCALATE.value:
        if decision.get("action") != "escalate":
            logger.info(
                "Coercing action %r → escalate (db_app_escalate policy)",
                decision.get("action"),
            )
            return _db_app_hil_decision(incident)
        return decision

    if state_manager.should_escalate(incident.id):
        return decision
    if decision.get("action") != "escalate":
        return decision

    spec = ACTION_MAP.get(incident.failure_type)
    if not spec:
        return decision

    primary = spec["primary"]
    fallback = spec.get("fallback", "escalate")
    svc = decision.get("service") or incident.service
    alt = fallback if fallback != primary else "escalate"

    ft = incident.failure_type

    if ft == FailureType.SERVICE_DOWN.value:
        out = {
            **decision,
            "action":          primary,
            "service":         svc,
            "error_summary":    (
                f"Service {svc} is classified as service_down (container status={incident.container_status!r}, "
                f"exit_code={incident.exit_code}). Automated recovery will restart the container."
            ),
            "root_cause":       (
                "The service container is not healthy or has stopped; restarting the container is the first-line recovery."
            ),
            "fix_explanation":  (
                "Running `docker restart` on the service container restores the process. "
                "This is executed by the ai-engine via the mounted Docker socket (not in your local shell)."
            ),
            "reasoning":        (
                "Policy: while retries remain, service_down is auto-healed with restart_service; "
                "escalate is reserved for exhausted retries."
            ),
            "alternative":      alt,
        }
        logger.info(
            "Coerced escalate → %s (service_down, service=%s status=%s exit=%s)",
            primary,
            svc,
            incident.container_status,
            incident.exit_code,
        )
        return out

    if ft == FailureType.DB_DOWN.value:
        out = {
            **decision,
            "action":          primary,
            "service":         svc,
            "error_summary":    (
                f"Database connectivity failure on {svc} (failure_type=db_down). "
                f"Automated recovery: {primary}."
            ),
            "root_cause":       (
                "The application cannot reach its database; restarting the DB container often clears transient connection or startup issues."
            ),
            "fix_explanation":  (
                f"`{primary}` restarts the database container so the service can reconnect."
            ),
            "reasoning":        (
                "Policy: while retries remain, db_down maps to restart_database instead of escalate."
            ),
            "alternative":      alt,
        }
        logger.info("Coerced escalate → %s (db_down, service=%s)", primary, svc)
        return out

    if ft == FailureType.ERROR_LOGS.value:
        out = {
            **decision,
            "action":          primary,
            "service":         svc,
            "error_summary":    (
                f"Application errors detected on {svc} (failure_type=error_logs). "
                f"Automated recovery: {primary}."
            ),
            "root_cause":       (
                "Runtime errors in logs; a container restart clears bad process state before deeper investigation."
            ),
            "fix_explanation":  (
                f"`{primary}` recycles the service process; use check_logs or rollback if restart does not clear the fault."
            ),
            "reasoning":        (
                "Policy: while retries remain, error_logs maps to restart_service instead of escalate."
            ),
            "alternative":      alt,
        }
        logger.info("Coerced escalate → %s (error_logs, service=%s)", primary, svc)
        return out

    if ft == FailureType.CODE_HEAL.value:
        out = {
            **decision,
            "action": primary,
            "service": svc,
            "error_summary": (
                f"Code contract failure on {svc} (failure_type=code_heal). "
                f"Automated recovery: {primary} (edit live source under CODE_HEAL_ROOT)."
            ),
            "root_cause": (
                "Logs match [code_heal]; the running app does not meet its health contract until source is corrected."
            ),
            "fix_explanation": (
                f"`{primary}` writes the corrected file and restarts the service so the process loads the fix."
            ),
            "reasoning": (
                "Policy: while retries remain, code_heal maps to fix_code instead of escalate."
            ),
            "alternative": alt,
        }
        logger.info("Coerced escalate → %s (code_heal, service=%s)", primary, svc)
        return out

    return decision


def _ensure_fix_code_payload(
    incident: Incident,
    decision: Dict,
    code_context: Dict[str, str],
) -> Dict:
    """If the LLM chose fix_code but omitted/truncated fix_file_content (common with large JSON),
    fill from mounted code_context with a minimal deterministic patch for the buggy-service demo.
    """
    if decision.get("action") != "fix_code":
        return decision
    if incident.failure_type != FailureType.CODE_HEAL.value:
        return decision
    raw = (decision.get("fix_file_content") or "").strip()
    if raw:
        return decision
    fix_file = (decision.get("fix_file") or "app.py").strip() or "app.py"
    base = (code_context or {}).get(fix_file)
    if not base:
        logger.warning(
            "fix_code with empty fix_file_content and no code_context[%s] — cannot synthesize patch",
            fix_file,
        )
        return decision
    fixed = base.replace("EXPECTED_MAGIC = 41", "EXPECTED_MAGIC = 42")
    if fixed == base:
        logger.warning(
            "fix_code fallback: no EXPECTED_MAGIC = 41 in %s — leaving decision unchanged",
            fix_file,
        )
        return decision
    logger.info(
        "Filled empty fix_file_content from code_context[%s] (demo EXPECTED_MAGIC patch)",
        fix_file,
    )
    return {**decision, "fix_file": fix_file, "fix_file_content": fixed}


def _try_parse_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    repaired = text.rstrip()
    if repaired.endswith(","):
        repaired = repaired[:-1]

    open_braces = repaired.count("{") - repaired.count("}")
    open_brackets = repaired.count("[") - repaired.count("]")

    in_string = False
    escape_next = False
    for ch in repaired:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string

    if in_string:
        repaired += '"'

    repaired += "]" * max(open_brackets, 0)
    repaired += "}" * max(open_braces, 0)

    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def _parse_response(raw: str, incident: Incident) -> Dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start:end + 1]
    elif start != -1:
        cleaned = cleaned[start:]

    data = _try_parse_json(cleaned)
    if data is None:
        logger.warning("LLM returned non-JSON — falling back to escalate")
        return _escalation_decision(incident)

    action = data.get("action", "escalate").strip().lower().replace(" ", "_")
    if action not in ALLOWED_ACTIONS:
        logger.warning("LLM suggested disallowed action '%s' — escalating", action)
        action = "escalate"

    return {
        "action": action,
        "service": data.get("service", incident.service),
        "error_summary": data.get("error_summary", ""),
        "root_cause": data.get("root_cause", ""),
        "fix_explanation": data.get("fix_explanation", ""),
        "reasoning": data.get("reasoning", ""),
        "confidence": data.get("confidence", "medium"),
        "alternative": data.get("alternative", "escalate"),
        "fix_file": data.get("fix_file", "") or "",
        "fix_file_content": data.get("fix_file_content", "") or "",
    }


# ---------------------------------------------------------------------------
# LangGraph node factories
# ---------------------------------------------------------------------------
def _make_classify_node(state_manager: StateManager):
    def classify(state: IncidentState) -> Dict:
        ft, kw, sev, tags = classify_failure(
            state["log_lines"], state["container_status"], state["exit_code"]
        )
        inc = Incident(
            service=state["service"],
            timestamp=state.get("timestamp", datetime.now().isoformat()),
            container_status=state["container_status"],
            exit_code=state["exit_code"],
            log_lines=state["log_lines"],
            failure_type=ft,
            error_keyword=kw,
            severity=sev,
            tags=tags,
        )
        state_manager.store_incident(inc)
        logger.info("Classified incident %s → %s (severity=%s)", inc.id, ft, sev)
        return {
            "incident_id": inc.id,
            "failure_type": ft,
            "error_keyword": kw,
            "severity": sev,
            "tags": tags,
        }
    return classify


def _make_analyze_node(llm, active_provider: str, state_manager: StateManager):
    def analyze(state: IncidentState) -> Dict:
        inc = state_manager.get_incident(state["incident_id"])
        state_manager.update_incident(inc.id, status=IncidentStatus.ANALYZING)

        if inc.failure_type == FailureType.DB_APP_ESCALATE.value:
            decision = _db_app_hil_decision(inc)
            logger.info("Skipping LLM for %s — db_app_escalate → human-in-the-loop", inc.id)
        elif state_manager.should_escalate(inc.id):
            logger.warning("Retry limit reached for %s — escalating", inc.id)
            decision = _escalation_decision(inc)
        else:
            prompt = _build_prompt(inc, state_manager, state.get("code_context") or {})
            try:
                raw = llm.generate(prompt)
                logger.debug("LLM raw response (first 300): %s", raw[:300])
                decision = _parse_response(raw, inc)
            except Exception as exc:
                logger.error("LLM error for %s: %s", inc.id, exc)
                decision = _escalation_decision(inc)

        decision = _coerce_self_heal_decision(inc, decision, state_manager)
        decision = _ensure_fix_code_payload(inc, decision, state.get("code_context") or {})

        state_manager.update_incident(
            inc.id,
            suggested_action=decision["action"],
            analysis_result=json.dumps(decision)[:1000],
            context={
                "llm_provider": active_provider,
                **{k: decision.get(k, "") for k in [
                    "error_summary", "root_cause", "fix_explanation", "reasoning", "confidence"
                ]},
            },
        )
        logger.info("Analyzed %s → action=%s (confidence=%s)",
                    inc.id, decision["action"], decision.get("confidence"))
        return {"decision": decision, "active_provider": active_provider}
    return analyze


def _make_execute_node(tool_manager: ToolManager):
    def execute(state: IncidentState) -> Dict:
        decision = state["decision"]
        action = decision["action"]
        service = decision.get("service") or state["service"]
        fallback = decision.get("alternative", "escalate")

        extra: Dict = {}
        if action == "fix_code":
            extra["fix_file"] = decision.get("fix_file")
            extra["fix_file_content"] = decision.get("fix_file_content")

        result = tool_manager.execute_with_fallback(
            state["incident_id"],
            primary_action=action,
            fallback_action=fallback if fallback != action else None,
            service=service,
            **extra,
        )
        logger.info("Tool result: %s", result)
        return {"tool_result": result.to_dict()}
    return execute


def _make_verify_node(tool_manager: ToolManager, state_manager: StateManager):
    def verify(state: IncidentState) -> Dict:
        service = (state["decision"].get("service") or state["service"])
        healed = tool_manager.verify_and_close(state["incident_id"], service)
        inc = state_manager.get_incident(state["incident_id"])
        return {
            "healed": healed,
            "retry_count": inc.retry_count,
            "final_status": inc.status.value,
        }
    return verify


def _make_escalate_node(state_manager: StateManager):
    def escalate(state: IncidentState) -> Dict:
        state_manager.update_incident(state["incident_id"], status=IncidentStatus.ESCALATED)
        logger.warning("Incident %s escalated to human", state["incident_id"])
        return {"final_status": IncidentStatus.ESCALATED.value, "healed": False}
    return escalate


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------
def _route_after_analyze(state: IncidentState) -> str:
    if state["decision"].get("action") == "escalate":
        return "escalate"
    return "execute"


def _route_after_verify(state: IncidentState) -> str:
    if state.get("healed"):
        return END
    if state.get("final_status") == IncidentStatus.ESCALATED.value:
        return "escalate"
    return "analyze"


# ---------------------------------------------------------------------------
# Agent: owns the LLM, builds and exposes the compiled LangGraph
# ---------------------------------------------------------------------------
class Agent:
    def __init__(
        self,
        state_manager: StateManager,
        tool_manager: ToolManager,
        llm_provider: str = "vertex_ai",
        fallback_providers: Optional[List[str]] = None,
    ):
        self.state_manager = state_manager
        self.tool_manager = tool_manager
        self.primary = llm_provider
        self.fallbacks = fallback_providers or ["gemini", "openai"]
        self.llm = None
        self.active_provider: str = ""
        self._init_llm()
        self.graph = self._build_graph()

    def _init_llm(self):
        for provider in [self.primary] + self.fallbacks:
            try:
                self.llm = _PROVIDERS[provider]()
                self.active_provider = provider
                logger.info("Using LLM provider: %s", provider)
                return
            except Exception as exc:
                logger.warning("Provider '%s' unavailable: %s", provider, exc)
        raise RuntimeError("No LLM provider could be initialised!")

    def _build_graph(self):
        workflow = StateGraph(IncidentState)

        workflow.add_node("classify", _make_classify_node(self.state_manager))
        workflow.add_node("analyze", _make_analyze_node(self.llm, self.active_provider, self.state_manager))
        workflow.add_node("execute", _make_execute_node(self.tool_manager))
        workflow.add_node("verify", _make_verify_node(self.tool_manager, self.state_manager))
        workflow.add_node("escalate", _make_escalate_node(self.state_manager))

        workflow.set_entry_point("classify")
        workflow.add_edge("classify", "analyze")
        workflow.add_conditional_edges(
            "analyze",
            _route_after_analyze,
            {"execute": "execute", "escalate": "escalate"},
        )
        workflow.add_edge("execute", "verify")
        workflow.add_conditional_edges(
            "verify",
            _route_after_verify,
            {"analyze": "analyze", "escalate": "escalate", END: END},
        )
        workflow.add_edge("escalate", END)

        return workflow.compile()

    def run(
        self,
        service: str,
        log_lines: List[str],
        container_status: str,
        exit_code: int,
        timestamp: Optional[str] = None,
        code_context: Optional[Dict[str, str]] = None,
    ) -> IncidentState:
        """
        Run the full incident response graph and return the final state.
        The state includes: incident_id, failure_type, decision, tool_result,
        healed, retry_count, final_status.
        """
        initial: IncidentState = {
            "service": service,
            "log_lines": log_lines,
            "container_status": container_status,
            "exit_code": exit_code,
            "timestamp": timestamp or datetime.now().isoformat(),
            "code_context": code_context or {},
            "incident_id": "",
            "failure_type": "",
            "error_keyword": "",
            "severity": "",
            "tags": [],
            "decision": {},
            "active_provider": "",
            "tool_result": {},
            "healed": False,
            "retry_count": 0,
            "final_status": "",
        }
        return self.graph.invoke(initial)


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
# Self-test (uses real LLM from .env)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    _handler = logging.StreamHandler()
    _handler.setFormatter(_JSONLogFormatter())
    logging.root.handlers = [_handler]
    logging.root.setLevel(logging.DEBUG)

    from ai_engine.tools import ToolManager as TM

    sm = StateManager()
    tm = TM(sm, dry_run=True)

    provider = os.getenv("LLM_PROVIDER", "local")
    fallbacks = os.getenv("LLM_FALLBACKS", "gemini,openai").split(",")
    agent = Agent(sm, tm, llm_provider=provider, fallback_providers=fallbacks)

    result = agent.run(
        service="payment-service",
        log_lines=["ERROR Connection refused: postgres://db:5432", "ERROR Retry 1/3 failed"],
        container_status="running",
        exit_code=0,
    )

    print(json.dumps({
        "test": "agent.py self-test",
        "incident_id": result["incident_id"],
        "failure_type": result["failure_type"],
        "action": result["decision"].get("action"),
        "healed": result["healed"],
        "final_status": result["final_status"],
        "llm_provider": result["active_provider"],
    }, indent=2))
