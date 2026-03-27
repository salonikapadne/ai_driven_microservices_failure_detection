import { useState, useEffect } from 'react';
import { io } from 'socket.io-client';
import { Activity, ShieldAlert, Cpu, CheckCircle, Search, Server, FilterX } from 'lucide-react';
import './index.css';

const socket = io('http://localhost:4000');

function App() {
  const [logs, setLogs] = useState([]);
  const [services, setServices] = useState({});
  const [rcaEvents, setRcaEvents] = useState([]);
  const [selectedService, setSelectedService] = useState(null);

  useEffect(() => {
    socket.on('initial_state', (state) => {
      if (state.logs) setLogs(state.logs);
      if (state.services) setServices(state.services);
      if (state.rcaEvents) setRcaEvents(state.rcaEvents);
    });

    socket.on('new_log', (logObj) => {
      setLogs((prev) => [logObj, ...prev].slice(0, 1000));
    });

    socket.on('status_update', (statusObj) => {
      setServices(statusObj);
    });

    socket.on('rca_event', (rcaEvent) => {
      setRcaEvents((prev) => [rcaEvent, ...prev].slice(0, 50));
    });

    return () => socket.off();
  }, []);

  const totalServices = Object.keys(services).length || 0;
  const runningServices = Object.values(services).filter(s => s.status === 'running').length;
  const failedServices = Object.values(services).filter(s => s.status !== 'running' && s.status !== 'unknown').length;
  const aiInterventions = rcaEvents.length;

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
              <div className="log-list">
                {filteredLogs.length > 0 ? filteredLogs.map((log, i) => (
                    <div key={i} className={`log-item ${log.exit_code !== 0 && log.exit_code !== null ? 'fatal' : ''}`}>
                        <span className="log-time">{new Date(log.timestamp).toLocaleTimeString()}</span>
                        <span className="log-service">{log.service}</span>
                        <div className="log-snippet">
                            {log.logs.join('\n')}
                        </div>
                    </div>
                )) : <p className="empty-state" style={{color: '#9ca3af', fontStyle:'italic'}}>No telemetry received yet...</p>}
              </div>
            </div>

            <div className="side-column">
              <div className="card service-health">
                <h3 style={{marginBottom: '0.5rem'}}>System Health</h3>
                <p style={{fontSize: '0.75rem', color: '#6b7280', marginBottom: '1.25rem'}}>Click a service below to isolate its logs streaming context natively</p>
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
                              userSelect: 'none'
                            }}
                          >
                              <div className="health-info">
                                  <strong>{name}</strong>
                                  <span style={{textTransform: 'uppercase'}}>{data.status || 'unknown'}</span>
                              </div>
                              {data.status === 'running' ? 
                                  <CheckCircle className="status-icon stable" /> : 
                                  (data.status === 'unknown' ? <Activity className="status-icon" color="#9ca3af" /> : <ShieldAlert className="status-icon fatal" />)
                              }
                          </div>
                      );
                    })}
                </div>
              </div>

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
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

export default App;
