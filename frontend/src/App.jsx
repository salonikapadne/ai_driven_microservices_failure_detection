import { useState, useEffect } from 'react';
import { io } from 'socket.io-client';
import { Activity, ShieldAlert, Cpu, CheckCircle, Search, Server, FilterX, Mail, X, Plus } from 'lucide-react';
import './index.css';

/** Dashboard Socket.IO URL — override with VITE_SOCKET_URL (e.g. http://192.168.1.10:4000 when not on localhost). */
const SOCKET_URL = import.meta.env.VITE_SOCKET_URL || 'http://localhost:4000';
const socket = io(SOCKET_URL, {
  transports: ['websocket', 'polling'],
  reconnectionDelay: 1000,
  reconnectionAttempts: 20,
});

/** Normalize ai_engine Socket.IO payloads into the same shape as log-collector messages. */
function aiEngineLogToTelemetry(entry) {
  const lines = [`[${entry.level}] ${entry.logger}: ${entry.message}`];
  if (entry.exc_info) lines.push(entry.exc_info);
  return {
    service: 'ai-engine',
    timestamp: entry.timestamp,
    container_status: 'running',
    exit_code: null,
    logs: lines,
    _fromAiEngine: true,
  };
}

function mergeInitialTelemetry(rmqLogs, aiEngineLogs) {
  const a = (rmqLogs || []).map((l) => ({ ...l, _fromAiEngine: false }));
  const b = (aiEngineLogs || []).map(aiEngineLogToTelemetry);
  return [...a, ...b]
    .sort((x, y) => new Date(y.timestamp) - new Date(x.timestamp))
    .slice(0, 1000);
}

function App() {
  const [logs, setLogs] = useState([]);
  const [services, setServices] = useState({});
  const [rcaEvents, setRcaEvents] = useState([]);
  const [selectedService, setSelectedService] = useState(null);
  const [alertEmails, setAlertEmails] = useState([]);
  const [emailInput, setEmailInput] = useState('');
  const [emailError, setEmailError] = useState('');

  useEffect(() => {
    const onInitialState = (state) => {
      const merged = mergeInitialTelemetry(state.logs, state.aiEngineLogs);
      setLogs(merged);
      if (state.services) setServices(state.services);
      if (state.rcaEvents) setRcaEvents(state.rcaEvents);
      if (state.alertEmails) setAlertEmails(state.alertEmails);
    };

    const onNewLog = (logObj) => {
      setLogs((prev) => [{ ...logObj, _fromAiEngine: false }, ...prev].slice(0, 1000));
    };

    const onStatusUpdate = (statusObj) => {
      setServices(statusObj);
    };

    const onRcaEvent = (rcaEvent) => {
      setRcaEvents((prev) => [rcaEvent, ...prev].slice(0, 50));
    };

    const onAiEngineLog = (entry) => {
      setLogs((prev) => [aiEngineLogToTelemetry(entry), ...prev].slice(0, 1000));
    };

    const onEmailListUpdate = ({ emails }) => {
      setAlertEmails(emails);
    };

    const onEmailError = ({ message }) => {
      setEmailError(message);
      setTimeout(() => setEmailError(''), 3000);
    };

    socket.on('initial_state', onInitialState);
    socket.on('new_log', onNewLog);
    socket.on('status_update', onStatusUpdate);
    socket.on('rca_event', onRcaEvent);
    socket.on('ai_engine_log', onAiEngineLog);
    socket.on('email_list_update', onEmailListUpdate);
    socket.on('email_error', onEmailError);

    return () => {
      socket.off('initial_state', onInitialState);
      socket.off('new_log', onNewLog);
      socket.off('status_update', onStatusUpdate);
      socket.off('rca_event', onRcaEvent);
      socket.off('ai_engine_log', onAiEngineLog);
      socket.off('email_list_update', onEmailListUpdate);
      socket.off('email_error', onEmailError);
    };
  }, []);

  const handleAddEmail = () => {
    const trimmed = emailInput.trim().toLowerCase();
    if (!trimmed) return;
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!emailRegex.test(trimmed)) {
      setEmailError('Please enter a valid email address');
      setTimeout(() => setEmailError(''), 3000);
      return;
    }
    if (alertEmails.includes(trimmed)) {
      setEmailError('This email is already in the list');
      setTimeout(() => setEmailError(''), 3000);
      return;
    }
    socket.emit('add_email', { email: trimmed });
    setEmailInput('');
    setEmailError('');
  };

  const handleRemoveEmail = (email) => {
    socket.emit('remove_email', { email });
  };

  const handleEmailKeyDown = (e) => {
    if (e.key === 'Enter') handleAddEmail();
  };

  const totalServices    = Object.keys(services).length || 0;
  const runningServices  = Object.values(services).filter(s => s.status === 'running').length;
  const failedServices   = Object.values(services).filter(s => s.status !== 'running' && s.status !== 'unknown').length;
  const aiInterventions  = rcaEvents.length;

  const filteredLogs = selectedService
    ? logs.filter(log => log.service === selectedService)
    : logs;

  return (
    <div className="layout">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="logo-container">
          <Activity className="logo-icon" />
          <span className="logo-text">MicroMonitor</span>
        </div>
        <nav className="nav-menu">
          <span className="nav-section">MAIN</span>
          <a href="#" className="nav-item active">
            <Cpu size={18} /> Dashboard
          </a>
          <a href="#" className="nav-item">
            <Server size={18} /> Services
          </a>
          <a href="#" className="nav-item">
            <ShieldAlert size={18} /> AI Analysis
          </a>
        </nav>
      </aside>

      {/* Main Content */}
      <main className="main-content">
        <header className="topbar">
          <div className="search-bar">
            <Search size={16} />
            <input type="text" placeholder="Search system events..." />
          </div>
          <div className="profile">
            <span className="profile-name">DevOps Admin</span>
            <div className="profile-img"></div>
          </div>
        </header>

        <div className="dashboard-content">
          <h1 className="page-title">Live Telemetry Dashboard</h1>
          <p className="page-subtitle">Real-time Root Cause Analysis & Autonomous Tracking.</p>

          <div className="stats-grid">
            <div className="stat-card primary">
              <h3>Total Microservices</h3>
              <div className="stat-val">{totalServices}</div>
              <span className="stat-trend badge">All integrated</span>
            </div>
            <div className="stat-card highlight">
              <h3>Healthy Services</h3>
              <div className="stat-val">{runningServices}</div>
              <span className="stat-trend badge">Up and running</span>
            </div>
            <div className="stat-card alert">
              <h3>Service Failures</h3>
              <div className="stat-val">{failedServices}</div>
              <span className="stat-trend badge alert">Currently down</span>
            </div>
            <div className="stat-card standard">
              <h3>AI Interventions</h3>
              <div className="stat-val">{aiInterventions}</div>
              <span className="stat-trend badge">Self-healed events</span>
            </div>
          </div>

          <div className="content-grid">
            <div className="card log-stream-card">
              <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem'}}>
                <h3 style={{margin:0}}>Live Telemetry {selectedService && `(${selectedService})`}</h3>
                {selectedService && (
                  <button
                    onClick={() => setSelectedService(null)}
                    style={{display: 'flex', gap: '5px', alignItems: 'center', background: '#fef2f2', color: '#dc2626', border: '1px solid #fca5a5', padding: '5px 10px', borderRadius: '5px', cursor: 'pointer', fontSize: '12px', fontWeight: 'bold'}}
                  >
                    <FilterX size={14}/> Clear Filter
                  </button>
                )}
              </div>
              <p className="log-stream-hint">Includes microservice logs from log-collector and AI engine (agent / state / tools) lines.</p>
              <div className="log-list">
                {filteredLogs.length > 0 ? filteredLogs.map((log, i) => (
                  <div
                    key={`${log.timestamp}-${log.service}-${i}`}
                    className={`log-item ${log.exit_code !== 0 && log.exit_code !== null ? 'fatal' : ''} ${log._fromAiEngine ? 'from-ai-engine' : ''}`}
                  >
                    <span className="log-time">{new Date(log.timestamp).toLocaleTimeString()}</span>
                    <span className="log-service">{log.service}</span>
                    <div className="log-snippet">
                      {log.logs.join('\n')}
                    </div>
                  </div>
                )) : <p className="empty-state" style={{color: '#9ca3af', fontStyle:'italic'}}>No telemetry received yet...</p>}
              </div>
            </div>

            {/* Right column */}
            <div className="side-column">

              {/* System Health */}
              <div className="card service-health">
                <h3 style={{marginBottom: '0.5rem'}}>System Health</h3>
                <p style={{fontSize: '0.75rem', color: '#6b7280', marginBottom: '1.25rem'}}>Click a service to isolate its logs</p>
                <div className="health-list">
                  {Object.entries(services).map(([name, data]) => {
                    const isSelected = selectedService === name;
                    return (
                      <div
                        key={name}
                        className={`health-item ${isSelected ? 'selected' : ''}`}
                        onClick={() => setSelectedService(isSelected ? null : name)}
                        style={{
                          cursor: 'pointer',
                          backgroundColor: isSelected ? '#f0fdf4' : 'transparent',
                          padding: isSelected ? '0.75rem' : '0.75rem 0',
                          borderRadius: isSelected ? '8px' : '0',
                          borderBottom: isSelected ? 'none' : '1px solid #e5e7eb',
                          transition: 'all 0.2s',
                          userSelect: 'none',
                        }}
                      >
                        <div className="health-info">
                          <strong>{name}</strong>
                          <span style={{textTransform: 'uppercase'}}>{data.status || 'unknown'}</span>
                        </div>
                        {data.status === 'running'
                          ? <CheckCircle className="status-icon stable" />
                          : (data.status === 'unknown'
                            ? <Activity className="status-icon" color="#9ca3af" />
                            : <ShieldAlert className="status-icon fatal" />)
                        }
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Recent AI Interventions */}
              <div className="card rca-events">
                <h3>Recent AI Interventions</h3>
                <div className="rca-list">
                  {rcaEvents.length > 0 ? rcaEvents.map((ev, i) => (
                    <div key={i} className="rca-item">
                      <div className="rca-header">
                        <span className="rca-service">{ev.service}</span>
                        <span className="rca-time">{new Date(ev.timestamp).toLocaleTimeString()}</span>
                      </div>
                      <p className="rca-desc">{ev.rca}</p>
                      <code className="rca-command">{ev.command}</code>
                    </div>
                  )) : <p className="empty-state" style={{color: '#9ca3af', fontStyle:'italic'}}>No failures autonomously patched yet.</p>}
                </div>
              </div>

              {/* Email Alerts */}
              <div className="card email-alerts-card">
                <div className="email-card-header">
                  <Mail size={18} className="email-card-icon" />
                  <h3 style={{margin: 0}}>Email Alerts</h3>
                  {alertEmails.length > 0 && (
                    <span className="email-count-badge">{alertEmails.length}</span>
                  )}
                </div>
                <p className="email-card-subtitle">
                  Receive an email whenever the AI detects and handles a failure.
                </p>

                {/* Add email input */}
                <div className="email-input-row">
                  <input
                    type="email"
                    className="email-input"
                    placeholder="recipient@example.com"
                    value={emailInput}
                    onChange={(e) => setEmailInput(e.target.value)}
                    onKeyDown={handleEmailKeyDown}
                  />
                  <button className="email-add-btn" onClick={handleAddEmail} title="Add email">
                    <Plus size={16} />
                  </button>
                </div>

                {/* Validation / server error */}
                {emailError && (
                  <p className="email-error">{emailError}</p>
                )}

                {/* Registered recipients */}
                {alertEmails.length > 0 ? (
                  <ul className="email-list">
                    {alertEmails.map((email) => (
                      <li key={email} className="email-list-item">
                        <span className="email-address">{email}</span>
                        <button
                          className="email-remove-btn"
                          onClick={() => handleRemoveEmail(email)}
                          title={`Remove ${email}`}
                        >
                          <X size={14} />
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="email-empty">No recipients yet. Add one above.</p>
                )}
              </div>

            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

export default App;
