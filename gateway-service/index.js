
const express = require('express');
const http = require('http');

const app = express();
const PORT = process.env.PORT || 3000;
const SERVICE_NAME = 'gateway-service';

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


function callService(url, trace_id) {
    return new Promise((resolve, reject) => {
        const req = http.get(url, { headers: { 'x-trace-id': trace_id } }, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => resolve(data));
        });
        req.on('error', reject);
    });
}


app.get('/health', (req, res) => {
    generateLog('INFO', 'Health check requested', req.trace_id);
    res.json({ status: 'UP', service: SERVICE_NAME });
});

app.get('/test', async (req, res) => {
    generateLog('INFO', 'Test endpoint requested', req.trace_id);
    
    try {
        await callService('http://user-service:3001/test', req.trace_id);
        await callService('http://order-service:3002/test', req.trace_id);
        await callService('http://payment-service:3003/test', req.trace_id);
        res.json({ message: 'All services tested successfully', service: SERVICE_NAME });
    } catch (e) {
        generateLog('ERROR', 'Failed to call downstreams', req.trace_id);
        res.status(500).json({ error: 'Failed to call downstreams' });
    }
    
});

app.get('/fail', async (req, res) => {
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
