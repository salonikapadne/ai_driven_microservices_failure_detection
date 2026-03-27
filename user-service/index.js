
const express = require('express');

const app = express();
const PORT = process.env.PORT || 3001;
const SERVICE_NAME = 'user-service';

function generateLog(level, message, trace_id) {
    const logObj = {
        timestamp: new Date().toISOString(),
        service: SERVICE_NAME,
        level,
        message,
        trace_id: trace_id || Math.random().toString(36).substring(7)
    };
    if (level === 'ERROR') {
        process.stderr.write(JSON.stringify(logObj) + '\n');
    } else {
        process.stdout.write(JSON.stringify(logObj) + '\n');
    }
}

app.use(express.json());
app.use((req, res, next) => {
    req.trace_id = req.headers['x-trace-id'] || Math.random().toString(36).substring(7);
    next();
});



app.get('/health', (req, res) => {
    generateLog('INFO', 'Health check requested', req.trace_id);
    res.json({ status: 'UP', service: SERVICE_NAME });
});

app.get('/test', (req, res) => {
    generateLog('INFO', 'Test endpoint requested', req.trace_id);
    res.json({ message: 'Test successful', service: SERVICE_NAME });
});

app.get('/fail', (req, res) => {
    const errorType = Math.random();
    generateLog('ERROR', 'Fail endpoint triggered', req.trace_id);
    if (errorType < 0.3) {
        generateLog('ERROR', 'DB timeout occurred', req.trace_id);
        res.status(500).json({ error: 'DB timeout' });
    } else if (errorType < 0.6) {
        generateLog('ERROR', 'Random internal error', req.trace_id);
        res.status(500).json({ error: 'Internal error' });
    } else {
        generateLog('ERROR', 'Simulating container crash', req.trace_id);
        res.status(500).json({ error: 'Crashing' });
        setTimeout(() => process.exit(1), 100);
    }
});

setInterval(() => {
    generateLog('INFO', 'Routine healthy heartbeats transmitted');
}, Math.floor(Math.random() * 5000) + 5000);

app.listen(PORT, () => {
    generateLog('INFO', `Service started on port ${PORT}`);
});
