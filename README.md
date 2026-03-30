# AI Microservices Failure Detection System

## Project Vision

In production microservice systems, **small failures cascade into big downtime** because humans need time to debug, investigate, and apply fixes. This project automates the entire failure detection and recovery pipeline using an AI agent that operates **with zero human intervention** for standard failure patterns.

**Core Philosophy**: We don't deploy real microservices. Instead, we simulate production-like failures using a synthetic log generator and focus all development energy on building an intelligent, autonomous agent that can diagnose and heal failures in real-time.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                    AI FAILURE DETECTION SYSTEM                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  log_generator.py ──→ observer.py ──→ state.py ──→ agent.py ──→  │
│  (Writes fake      (Reads new     (Creates    (Asks Gemini/  │
│   failures)        failure)       incident    OpenAI what    │
│                                   state)      to do)          │
│                                                    ↓           │
│                                              tools.py         │
│                                              (Executes fix)   │
│                                                    ↓           │
│                                            health_check.py    │
│                                                    ↓           │
│                                         ┌─ fixed? ─┐          │
│                                         │          │          │
│                                       YES         NO          │
│                                         │          │          │
│                                      HEALED    RETRY/         │
│                                                ESCALATE       │
│                                         │          │          │
│                                         └──────────┘          │
│                                              ↓                │
│                                         dashboard/            │
│                           (React - Shows everything)          │
│                                                                │
└─────────────────────────────────────────────────────────────────────┘
```

---

## File Ownership & Responsibilities

### Dev 1: Core State Management
- **main.py** - Application entry point; orchestrates startup and shutdown; initializes all components
- **state.py** - Incident state machine; tracks failure detection, recovery attempts, and outcomes; persists state to memory
- **observer.py** - Event listener; watches log generator output; converts raw logs into incidents; triggers agent

### Dev 2: Tool Execution Engine
- **tools.py** - Executes recovery actions recommended by the AI agent; handles service restarts, config updates, resource allocation; reports execution status back to agent

### Dev 3: Intelligent Agent & Prompts
- **agent.py** - Core AI orchestration; queries Gemini/OpenAI LLM with incident context; parses LLM responses into executable tools; implements retry logic and escalation
- **prompts.txt** - System prompts and context templates; defines agent personality, knowledge constraints, recovery patterns; examples of successful recoveries

### Dev 4: Synthetic Failure Generation
- **log_generator.py** - Produces realistic failure logs in the JSON format specified; simulates various failure modes (memory leaks, network timeouts, database connection failures, etc.); runs continuously in background

### Dev 5: Frontend & Visualization
- **dashboard/** (React) - Real-time incident visualization; shows active failures, suggested fixes, recovery attempts, historical trends; provides manual override capabilities

---

## Log Format Specification

All logs follow this standardized JSON format:

```json
{
  "service": "payment-service",
  "timestamp": "2026-03-27T14:32:01.234567Z",
  "container_status": "running | exited",
  "exit_code": 0,
  "logs": [
    "2026-03-27 14:31:55 ERROR [payment-api] Connection timeout to database",
    "2026-03-27 14:31:56 ERROR [payment-api] Retry attempt 1 failed",
    "2026-03-27 14:31:57 ERROR [payment-api] Retry attempt 2 failed",
    "2026-03-27 14:31:58 INFO [payment-api] Circuit breaker opened"
  ]
}
```

### Field Definitions:
- **service**: Service name (e.g., `payment-service`, `user-service`, `order-service`)
- **timestamp**: ISO 8601 timestamp when the failure was detected
- **container_status**: Either `running` (service still up) or `exited` (service crashed)
- **exit_code**: Exit code if container exited; 0 for success, non-zero for errors
- **logs**: Array of log lines captured from the service; typically 3-10 lines showing the failure progression

---

## Data Flow: From Failure to Recovery

### 1️⃣ Failure Generation
```python
# log_generator.py produces:
{
  "service": "auth-service",
  "timestamp": "2026-03-27T14:35:22.123456Z",
  "container_status": "exited",
  "exit_code": 137,
  "logs": [ "OOM Killer invoked", "Out of memory" ]
}
```

### 2️⃣ Failure Detection
```
observer.py polls log_generator or listens to event stream
→ Detects new failure entry
→ Creates Incident object
→ Triggers incident_detected event
```

### 3️⃣ State Creation & Tracking
```
state.py receives incident
→ Assigns unique incident_id
→ Records detection_time, service_name, log_context
→ Sets status = "DETECTED"
→ Status flow: DETECTED → ANALYZING → EXECUTING → VERIFIED/ESCALATED
```

### 4️⃣ Agent Analysis & Planning
```
agent.py receives incident state
→ Formats prompt with: service name, logs, historical context
→ Sends to Gemini/OpenAI: "Service X failed with [logs]. What to do?"
→ LLM responds with: action plan (e.g., "restart service", "scale up", "clear cache")
→ Parses response into tool calls
```

### 5️⃣ Tool Execution
```
tools.py executes actions:
  - restart_service(service_name)
  - scale_replicas(service_name, count)
  - clear_cache(service_name)
  - update_config(service_name, key, value)
  - drain_connections(service_name)
→ Returns: success/failure, duration, output logs
```

### 6️⃣ Health Verification
```
health_check() verifies:
  - Service is responding to requests
  - Response times within SLA
  - Error rates below threshold
→ Returns: healthy/unhealthy
```

### 7️⃣ Outcome & Resolution
```
if health_check() == healthy:
  state.status = "HEALED"
  log success, notify dashboard
else:
  Retry with different action OR escalate_to_humans()
```

### 8️⃣ Dashboard Display
```
dashboard/ queries state API
→ Shows real-time incident list
→ Displays recommended actions
→ Tracks recovery duration
→ Provides historical analytics
```

---

## Project Setup

### Prerequisites
- Python 3.9+
- Node.js 16+ (for dashboard)
- Gemini/OpenAI API key (set as environment variable)
- Git

### Installation

1. **Clone the repository**
   ```bash
   cd c:\sallyworkspace
   git clone <repo-url> ai_microservices_failure_detection
   cd ai_microservices_failure_detection
   ```

2. **Set up Python environment**
   ```bash
   python -m venv venv
   .\venv\Scripts\activate  # Windows
   pip install -r requirements.txt
   ```

3. **Configure environment**
   ```bash
   # Create .env file
   echo GEMINI_API_KEY=your_key_here > .env
   echo OPENAI_API_KEY=your_key_here >> .env
   echo LOG_GENERATOR_INTERVAL=5 >> .env
   echo DASHBOARD_PORT=3000 >> .env
   ```

4. **Set up dashboard**
   ```bash
   cd dashboard
   npm install
   cd ..
   ```

---

## Running the System

### Start All Components
```bash
# In one terminal - Python backend
python main.py

# In another terminal - React dashboard
cd dashboard
npm start
```

### Run Individual Components (Development)

**Dev 1 - Test state management:**
```bash
python -m pytest tests/test_state.py -v
```

**Dev 2 - Test tools execution:**
```bash
python -m pytest tests/test_tools.py -v
python tools.py --simulate-fix  # Manual testing
```

**Dev 3 - Test agent decision-making:**
```bash
python -m pytest tests/test_agent.py -v
python agent.py --dry-run  # See what agent would do without executing
```

**Dev 4 - Generate sample logs:**
```bash
python log_generator.py --samples 10 --output logs.json
```

**Dev 5 - Run dashboard only:**
```bash
cd dashboard && npm start
```

---

## Configuration

### `config.yaml` - Central Configuration
```yaml
agent:
  model: "gemini-2.0"  # or "gpt-4"
  temperature: 0.3
  max_tokens: 500

tools:
  execution_timeout: 30  # seconds
  enable_dry_run: false
  retry_count: 3

health_check:
  interval: 10  # seconds
  timeout: 5
  endpoint: "/health"

log_generator:
  interval: 5  # seconds between new failures
  services:
    - payment-service
    - user-service
    - order-service
    - auth-service
  failure_modes:
    - memory_leak
    - timeout
    - db_connection_failed
    - crash
```

---

## API Endpoints

### Backend REST API (Flask/FastAPI)
```
GET  /api/incidents              # List all incidents
GET  /api/incidents/{id}         # Get incident details
GET  /api/incidents/{id}/actions # Suggested actions for an incident
POST /api/incidents/{id}/approve # Approve and execute suggested action
GET  /api/health                 # System health status
GET  /api/metrics                # Success rate, avg recovery time, etc.
```

### Dashboard Connections
- WebSocket for real-time incident updates
- Polling fallback for environments without WebSocket support

---

## Key Concepts & Patterns

### Incident Lifecycle
```
DETECTED (new failure) 
  ↓
ANALYZING (agent thinking)
  ↓
EXECUTING (tools running)
  ↓
VERIFIED (health check passed) → HEALED ✓
ESCALATED (couldn't auto-fix) → human review → manual fix
```

### Agent Decision Input
The agent receives this context for every incident:
- Service name
- Full log lines (3-10 entries)
- Service health metrics
- Previous incidents for this service (pattern detection)
- Available recovery tools

### Recovery Patterns
Common patterns the agent learns to handle:
1. **Memory Leak** → Restart service, monitor memory
2. **Connection Timeout** → Restart dependent service, increase timeout
3. **Database Connection Lost** → Wait + retry OR failover to replica
4. **Rate Limited** → Scale up replicas, throttle client requests
5. **Crash Loop** → Check logs, rollback recent changes, escalate if necessary

---

## Development Guidelines

### Code Organization
```
ai_microservices_failure_detection/
├── main.py                    # App entry point (Dev 1)
├── state.py                   # State management (Dev 1)
├── observer.py                # Event listener (Dev 1)
├── agent.py                   # AI orchestration (Dev 3)
├── prompts.txt                # LLM prompts (Dev 3)
├── tools.py                   # Action execution (Dev 2)
├── log_generator.py           # Fake failures (Dev 4)
├── config.yaml                # Configuration
├── requirements.txt           # Python dependencies
├── dashboard/                 # React frontend (Dev 5)
│   ├── src/
│   ├── public/
│   └── package.json
├── tests/                     # Unit tests
│   ├── test_state.py
│   ├── test_agent.py
│   ├── test_tools.py
│   └── test_observer.py
└── docs/                      # Additional documentation
```

### Testing Strategy
- **Unit tests**: Each developer tests their module in isolation
- **Integration tests**: Test interactions between modules (main.py coordinates)
- **E2E tests**: Full flow from failure generation to dashboard display

### Before Committing
```bash
# Run all tests
python -m pytest tests/ -v

# Check code quality
flake8 *.py
black *.py --check

# Type checking
mypy agent.py state.py tools.py
```

### Logging & Debugging
```python
import logging
logger = logging.getLogger(__name__)

# All modules log their actions
logger.info(f"Incident detected: {incident_id}")
logger.error(f"Tool execution failed: {error}")
logger.debug(f"Agent decision: {decision}")
```

---

## Troubleshooting

### Agent Makes Wrong Decisions
1. Check `prompts.txt` - Add more context about the failure pattern
2. Review logs in `state.py` - Did it correctly understand the incident?
3. Test with `agent.py --dry-run` to see reasoning

### Tools Fail to Execute
1. Verify tool availability: `python tools.py --list`
2. Check permissions for service restarts
3. Ensure dependencies are installed

### Health Check Always Fails
1. Verify health endpoint is correct in `config.yaml`
2. Check if service actually started (check logs)
3. Increase timeout if service is slow to start

### Dashboard Not Updating
1. Check WebSocket connection: `browser console` for errors
2. Verify backend is running: `curl http://localhost:5000/api/health`
3. Check CORS settings if frontend and backend on different ports

---

## Performance Targets

| Metric | Target | How to Measure |
|--------|--------|----------------|
| Failure Detection Latency | < 2 seconds | Time from failure generation to incident creation |
| Agent Response Time | < 5 seconds | Time from incident to LLM response |
| Tool Execution Time | < 10 seconds | Time to restart/fix service |
| Health Verification | < 5 seconds | Time for health check to complete |
| **Total Recovery Time** | **< 30 seconds** | From failure to healed status |

---

## Security Considerations

- ✅ **API Keys**: Store in `.env`, never in code or git
- ✅ **Tool Execution**: Whitelist allowed recovery actions
- ✅ **LLM Responses**: Sanitize and validate before executing commands
- ✅ **Incident Data**: May contain sensitive logs - handle appropriately
- ✅ **Dashboard Access**: Implement authentication if exposed publicly

---

## Future Enhancements

- [ ] Machine learning to predict failures before they happen
- [ ] Multi-region failure coordination
- [ ] Automatic playbook generation from past incidents
- [ ] Slack/Email notifications for escalations
- [ ] Custom metrics dashboard
- [ ] Incident correlation (find root cause across services)
- [ ] Cost optimization (intelligent auto-scaling)

---

## Detailed Technical Guides

This project includes comprehensive implementation guides:

- **[STATE_MANAGEMENT_GUIDE.md](STATE_MANAGEMENT_GUIDE.md)** - Complete state.py + observer.py implementation with state formation for your 3 error types (DB Down, Service Down, Error Logs)

- **[VERTEX_AI_SETUP.md](VERTEX_AI_SETUP.md)** - Step-by-step Google Cloud Vertex AI setup, authentication, plus Agent.py code that supports Vertex AI, Gemini, and OpenAI with fallback logic

- **[TOOLS_EXECUTION_GUIDE.md](TOOLS_EXECUTION_GUIDE.md)** - Complete tools.py implementation with tool definitions for restart_service, restart_database, rollback_deployment, scale_replicas, and more

- **[END_TO_END_FLOW.md](END_TO_END_FLOW.md)** - Complete walkthrough of the failure → recovery flow with code examples for all 3 error types, showing exact state transitions and logging output

## Support & Debugging

### Enable Debug Logging
```bash
export LOG_LEVEL=DEBUG
python main.py
```

### Simulate a Failure
```bash
python log_generator.py --failure-mode memory_leak --service payment-service
```

### Manual Agent Testing
```python
from agent import Agent
from state import Incident

agent = Agent()
test_incident = Incident(
    service="test",
    logs=["Connection timeout", "Retry failed"]
)
action = agent.analyze(test_incident)
print(f"Suggested action: {action}")
```

---

## Team Coordination

### Daily Standup Checklist
- [ ] What incidents were detected today?
- [ ] What recovery patterns are new?
- [ ] Are there any recurring failures?
- [ ] Dashboard metrics - trending up or down?

### Integration Points (Where Devs Work Together)
1. **Dev 1 ↔ Dev 4**: State.py consumes logs from log_generator.py
2. **Dev 1 ↔ Dev 3**: Observer.py triggers agent.py when incident detected
3. **Dev 3 ↔ Dev 2**: Agent.py calls tools.py with recovery actions
4. **Dev 2 ↔ Dev 1**: Tools.py updates state.py with execution results
5. **Everyone ↔ Dev 5**: Dashboard reads from all APIs and state

---

## License & Contributors

**Contributors:**
- Dev 1: State Management & Core Orchestration
- Dev 2: Tool Execution Engine
- Dev 3: AI Agent & LLM Integration
- Dev 4: Failure Simulation
- Dev 5: Dashboard & Visualization

---

## Questions?

For architecture questions → Review the flow diagram above
For ownership questions → See "File Ownership & Responsibilities"
For incident format questions → See "Log Format Specification"
For setup issues → See "Troubleshooting"

Happy automating! 🚀
