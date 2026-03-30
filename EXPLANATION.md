# System explanation — presentation guide (5 speakers)

This document summarizes **what the stack does**, **the theory behind it**, and **how to split a team presentation** across five people. It reflects the current codebase: Docker Compose microservices, **RabbitMQ** log bus, **log-collector** (Docker log tail), **ai-engine** (Flask-SocketIO **dashboard** + **LangGraph agent** + **tools**), **mail-service**, **frontend**, and demo services (**hil-db-demo**, **buggy-service**).

---

## Presentation split (5 people)

| Presenter(s) | Focus area | Primary artifacts |
|----------------|------------|-------------------|
| **Person 1 & 2** (2 people) | **AI engine & tools** | `ai_engine/agent.py`, `ai_engine/state.py`, `ai_engine/tools.py`, `consumer.py` (optional) |
| **Person 3** (1 person) | **Research, frontend & email** | LLM providers & prompts, `frontend/`, `mail-service/`, alerting flow |
| **Person 4 & 5** (2 people) | **Platform & integration** | `dashboard.py`, `docker-compose.yml`, `log-collector/`, demo services (`hil-db-demo/`, `buggy-service/`), RabbitMQ, volumes, env |

Below, sections are tagged with **【P1–P2】**, **【P3】**, **【P4–P5】** so each group can rehearse independently.

---

## End-to-end flow (everyone should understand this)

1. **Containers** run sample microservices; **log-collector** tails `docker logs` per service and publishes JSON batches to **RabbitMQ** (`logs_queue`).
2. **ai-engine** runs **`dashboard.py`**: a background thread **consumes** the queue, updates in-memory state, pushes events over **Socket.IO** to the **React frontend**.
3. When a batch looks like a **failure** (`_needs_analysis` in `dashboard.py`), the dashboard spawns **`agent.run(...)`**: **classify → analyze (LLM) → execute (tools) → verify →** retry or end.
4. **Tools** use the **Docker socket** (e.g. `docker restart`) and, for the **code-heal demo**, write files under a **mounted volume** and restart the demo service.
5. **RCA / alerts**: results are emitted as Socket.IO events; optional **email** via **SMTP** or **mail-service** (Resend/Gmail).

**Theory:** This is a classic **observe → decide → act → verify** loop, similar to **AIOps** / **closed-loop remediation**, with a **policy layer** (classifier + allowed actions) instead of unconstrained shell access.

---

# 【P1–P2】AI engine & tools (two presenters)

Suggested split: **P1** = graph + prompts + LLM integration; **P2** = state machine + classifier + tools + safety.

## 1. `ai_engine/state.py` — incident model & classification

**What it does**

- Defines **`Incident`**: service name, logs, **`failure_type`**, severity, **`retry_count`**, status (`DETECTED`, `ANALYZING`, `EXECUTING`, `HEALED`, `ESCALATED`, `RETRY`).
- **`classify_failure(log_lines, container_status, exit_code)`** maps raw text to a **`FailureType`** enum (`db_down`, `db_app_escalate`, **`code_heal`**, `service_down`, `error_logs`, `unknown`).
- Order matters: e.g. **HIL markers** and **`[code_heal]`** are checked **before** generic DB keywords so demos don’t get misclassified as `db_down`.

**Theory**

- **Rule-based classification** is fast and deterministic; the LLM then explains and chooses tools **within** that type.
- **Human-in-the-loop (HIL)** types (`db_app_escalate`) intentionally **never** auto-restart DB — policy encodes “needs human”.
- **`code_heal`** ties log markers to a **source-edit** path (see `buggy-service`).

## 2. `ai_engine/agent.py` — LangGraph orchestration

**What it does**

- Builds a **StateGraph**: `classify → analyze → (execute | escalate) → verify → END / retry / escalate`.
- **`_build_prompt`**: injects service context, log lines, optional **`code_context`** (file snippets from disk for code-heal).
- **`_parse_response`**: expects **JSON** from the LLM (`action`, `fix_file`, `fix_file_content`, etc.).
- **`_coerce_self_heal_decision`**: if the model says **`escalate`** but retries remain, **coerce** to the **primary action** from **`ACTION_MAP`** (e.g. `restart_service` for `error_logs`, **`fix_code`** for `code_heal`).
- **`_ensure_fix_code_payload`**: if `fix_code` is chosen but **`fix_file_content`** is empty (common with large JSON), **fill from `code_context`** with a **deterministic demo patch** (`EXPECTED_MAGIC` fix) so the pipeline can still heal.

**Theory**

- **LangGraph** models remediation as a **finite state machine** with explicit branches — easier to test than a single giant prompt.
- **Structured output** (JSON) is easier to validate than free text.
- **Coercion** reduces false escalations when small models default to “escalate”.

## 3. `ai_engine/tools.py` — actuators & critic

**What it does**

- **`TOOL_REGISTRY`**: `restart_service`, `restart_database`, `check_logs`, `rollback_deployment`, `scale_replicas`, **`fix_code`**, `read_file`, `run_cmd` (allowlisted).
- **`ACTION_MAP`**: maps **`failure_type` → { primary, fallback }** (e.g. `code_heal` → **`fix_code`**, fallback **`restart_service`**).
- **`fix_code`**: resolves paths under **`CODE_HEAL_ROOT`**, writes **`CODE_HEAL_FILES`**, runs **`docker restart`**, then **HTTP health** check with **retries** (Flask startup race).
- **`verify_and_close`**: after tools run, marks **`HEALED`** or increments **`retry_count`**; at **`MAX_RETRIES`**, status **`ESCALATED`**.

**Theory**

- **Least privilege**: only allowlisted paths/commands; no arbitrary shell.
- **Idempotency & verification**: same pattern as **Kubernetes** health checks after rollout.
- **Fallback actions** mirror ops playbooks (restart → scale → rollback).

## 4. Optional: `consumer.py`

Separate RabbitMQ consumer path (if used); often the **dashboard consumer** is the main integration in this repo.

**Talking points for P1 vs P2**

- **P1:** Graph topology, prompts, JSON schema, `fix_code` + LLM limitations, coercion and fallback payload.
- **P2:** Classifier rules, `StateManager` / retries / escalation, Docker tools, `CODE_HEAL_*` env safety, health checks.

---

# 【P3】Research, frontend & email (one presenter)

## 1. LLM research & configuration

**What to cover**

- **Providers** in `agent.py`: Vertex AI, Gemini API, OpenAI, Ollama (`OLLAMA_URL`).
- Env vars: `LLM_PROVIDER`, `LLM_FALLBACKS`, keys, `VERTEX_AI_*`, etc.
- **Why fallbacks matter**: quota, latency, JSON validity.

**Theory**

- **Temperature** low for deterministic JSON.
- **Token limits** truncate long `fix_file_content` — motivates **dashboard-loaded `code_context`** + **deterministic fallback patch** in code.

## 2. `frontend/` — React + Socket.IO

**What it does**

- Connects to **`VITE_SOCKET_URL`** (dashboard on port **4000**).
- Subscribes to **`new_log`**, **`status_update`**, **`rca_event`**, **`ai_engine_log`**, etc.
- Displays **Recent AI Interventions** (RCA text, command, optional **escalation** styling).

**Theory**

- **Socket.IO** = real-time push; fits **event-sourced** monitoring UIs.
- Separation: **no AI logic in frontend** — display only.

## 3. Email: `mail-service/` + dashboard SMTP

**What it does**

- **`dashboard.py`**: if **`MAILER_URL`** + **`MAILER_INTERNAL_TOKEN`** are set, POST **`/send-alert`** to **mail-service** (Nodemailer); else **SMTP** via `EMAIL_USER` / `EMAIL_PASS`.
- Recipients: **`EMAIL_ALERT_RECIPIENTS`** + in-UI list.

**Theory**

- **Sidecar pattern**: isolate email credentials from Python process.
- **Escalation emails** vs **healed** emails differ by **`alert_kind`** in payload.

---

# 【P4–P5】Platform & integration (two presenters)

Suggested split: **P4** = data path (logs, queue, collector, compose); **P5** = dashboard behavior, demos, volumes, ops.

## 1. `dashboard.py` — bridge & policy gate

**What it does**

- Flask-SocketIO server; **RabbitMQ consumer thread** reads **`logs_queue`**.
- **`_needs_analysis`**: decides if a batch should trigger **`agent.run`**. Uses **`_FAILURE_KEYWORDS`** plus **extra markers** (e.g. **`[code_heal]`**, Werkzeug **` 500 -`**) because some failure lines don’t contain the word “error”.
- **`_load_code_context`**: for services in **`CODE_HEAL_SERVICES`**, reads files from **`CODE_HEAL_ROOT`** and passes **`code_context`** into **`agent.run`**.
- **Cooldown** per service to avoid thrashing.

**Theory**

- **Dual pipeline**: “show all logs” vs “expensive AI run” — gate reduces cost and noise.
- **Volume mount** must align: ai-engine and buggy-service share **`buggy-service/live`** so edits persist.

## 2. `log-collector/` — Docker log shipping

**What it does**

- Node script: for each **service name**, tails logs, attaches **container stats**, publishes JSON to RabbitMQ.

**Theory**

- **Sidecar / daemon** pattern: centralized observability without changing app code.

## 3. `docker-compose.yml` — topology

**What to cover**

- Services: microservices, **hil-db-demo**, **buggy-service**, **rabbitmq**, **log-collector**, **mail-service**, **ai-engine**, **frontend**.
- **ai-engine**: mounts **Docker socket**, **vertex key**, **`./buggy-service/live:/buggy-live`**, env **`CODE_HEAL_*`**, **`BUGGY_SERVICE_HEALTH_URL`**.

**Theory**

- **Docker Compose** = reproducible demo environment.
- **Named containers** must match **log-collector**’s service list.

## 4. Demo: `hil-db-demo/`

**What it does**

- Shell script phases: benign ticks, then **HIL** markers (`[hil_db_demo]`, etc.) for **`db_app_escalate`** — **escalate**, no Docker restart.

**Theory**

- Demonstrates **policy** overriding automation when **risk** is high.

## 5. Demo: `buggy-service/`

**What it does**

- **`seed/app.py`**: intentional bug (`EXPECTED_MAGIC = 41`); prints **`[code_heal]`** on bad `/health`.
- **`entrypoint.sh`**: copies **seed → live** on every container start (**reset** for repeat demos).
- Host mount **`live/`** shared with **ai-engine** for **`fix_code`**.

**Theory**

- **Immutable seed / mutable live** = deterministic **reset** (`docker compose restart buggy-service`).

---

## Suggested presentation order (25–40 min total)

1. **P4–P5 (5–8 min):** Architecture diagram — Compose, RabbitMQ, log-collector, dashboard, frontend.
2. **P1–P2 (10–15 min):** State → agent graph → tools; live trace: one log batch → classify → fix_code.
3. **P3 (5–8 min):** LLM choices, frontend live view, email path.
4. **All (5 min):** Demo script — HIL vs code-heal; Q&A.

---

## Glossary (theoretical)

| Term | Meaning here |
|------|----------------|
| **AIOps** | AI for IT operations — anomaly detection, RCA, remediation. |
| **Closed loop** | Observe → act → verify without manual step. |
| **LangGraph** | Graph-based agent workflow (nodes + conditional edges). |
| **Policy / guardrails** | Allowlisted actions, failure types, max retries, HIL types. |
| **RCA** | Root cause analysis text shown in the UI. |
| **Socket.IO** | Bidirectional real-time channel over HTTP. |

---

## File index (quick reference)

| Path | Role |
|------|------|
| `ai_engine/state.py` | Incidents, classifier |
| `ai_engine/agent.py` | LangGraph, prompts, JSON parsing |
| `ai_engine/tools.py` | Docker + `fix_code`, health |
| `dashboard.py` | Queue consumer, Socket.IO, `agent.run`, email trigger |
| `log-collector/index.js` | Tail logs → RabbitMQ |
| `docker-compose.yml` | Full stack |
| `frontend/src/` | React UI |
| `mail-service/index.js` | Email API |
| `hil-db-demo/` | HIL escalation demo |
| `buggy-service/` | Code self-heal demo |

---

*This document is for internal presentation and onboarding; adjust depth to your audience.*
