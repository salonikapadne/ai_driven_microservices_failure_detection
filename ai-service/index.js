const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const cors = require('cors');
const amqp = require('amqplib');
const { GoogleGenerativeAI } = require('@google/generative-ai');
const { exec } = require('child_process');
const nodemailer = require('nodemailer');

const app = express();
app.use(cors());
const server = http.createServer(app);
const io = new Server(server, { cors: { origin: '*' } });

// Configure NodeMailer transporter
const transporter = nodemailer.createTransport({
    service: 'gmail',
    auth: {
        user: process.env.EMAIL_USER,
        pass: process.env.EMAIL_PASS
    }
});

async function sendEmailAlert(service, rca, command) {
    const mailOptions = {
        from: process.env.EMAIL_USER || 'alert@microservices-sim.local',
        to: 'sajoshi06@gmail.com',
        subject: `🚨 Alert: Error/Crash in microservice ${service}`,
        text: `An error or crash occurred in service: ${service}\n\nRoot Cause Analysis:\n${rca}\n\nSuggested/Executed Command:\n${command}\n\nTime of encounter:\n${new Date().toISOString()}`,
        html: `<h2>🚨 Microservice Alert: ${service}</h2>
               <p>An error or crash occurred in <strong>${service}</strong>.</p>
               <h3>Root Cause Analysis:</h3>
               <p>${rca}</p>
               <h3>Suggested/Executed Healing Command:</h3>
               <pre><code>${command}</code></pre>
               <p><small>Time of encounter: ${new Date().toISOString()}</small></p>`
    };

    try {
        const info = await transporter.sendMail(mailOptions);
        console.log(`[EMAIL ALERT] Sent to sajoshi06@gmail.com: ${info.messageId}`);
    } catch (error) {
        console.error(`[EMAIL ALERT ERROR] Failed to send email: ${error.message}`);
    }
}

const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);
let channel = null;

const state = {
    logs: [],
    rcaEvents: [],
    services: {
        'user-service': { status: 'unknown', exitCode: null, lastSeen: Date.now() },
        'order-service': { status: 'unknown', exitCode: null, lastSeen: Date.now() },
        'payment-service': { status: 'unknown', exitCode: null, lastSeen: Date.now() },
        'gateway-service': { status: 'unknown', exitCode: null, lastSeen: Date.now() }
    }
};

const MAX_LOGS = 1000;

io.on('connection', (socket) => {
    socket.emit('initial_state', state);
});

async function connectRabbitMQ() {
    try {
        const conn = await amqp.connect('amqp://rabbitmq');
        channel = await conn.createChannel();
        await channel.assertQueue('logs_queue');
        console.log('AI Service Backend: Connected to RabbitMQ!');
        
        channel.consume('logs_queue', async (msg) => {
            if (msg !== null) {
                const rawLog = msg.content.toString();
                channel.ack(msg);
                try {
                    const logObj = JSON.parse(rawLog);
                    await processLog(logObj);
                } catch (e) {}
            }
        });
    } catch (e) {
        setTimeout(connectRabbitMQ, 5000);
    }
}

function executeHealingSequence(command, service) {
    console.log(`\n⚙️ [HEALING ENGINE] Executing automated recovery command for ${service}...`);
    console.log(`> ${command}`);
    
    exec(command, (err, stdout, stderr) => {
        if (err) {
             console.error(`[HEALING ERROR] Command failed: ${err.message}`);
             return;
        }
        if (stderr && stderr.trim().length > 0) {
             console.warn(`[HEALING WARNING] Stderr: ${stderr.trim()}`);
        }
        console.log(`[HEALING SUCCESS] Clean recovery applied! Output: ${stdout.trim()}\n`);
    });
}

const coolingDown = {};

async function processLog(logObj) {
    state.logs.unshift(logObj);
    if(state.logs.length > MAX_LOGS) state.logs.pop();

    state.services[logObj.service] = {
        status: logObj.container_status,
        exitCode: logObj.exit_code,
        lastSeen: Date.now()
    };

    io.emit('new_log', logObj);
    io.emit('status_update', state.services);

    if (coolingDown[logObj.service] && (Date.now() - coolingDown[logObj.service]) < 45000) return;
    
    let needsRCA = false;
    if (logObj.exit_code !== 0 && logObj.exit_code !== null) needsRCA = true;
    if (!needsRCA) {
        for (const line of logObj.logs) {
            if (line.includes('"level":"ERROR"')) { needsRCA = true; break; }
        }
    }
    
    if (needsRCA) {
        coolingDown[logObj.service] = Date.now();
        console.log(`\n🚨 [AI RCA TRIGGERED] Failure detected in upstream service: ${logObj.service}`);
        console.log(`Transmitting telemetry to Gemini AI for Root Cause Analysis and Self-Healing proposals...\n`);

        const prompt = `You are an expert DevOps AI Healing Engine. Analyze this JSON log from a failing microservice.
Context: ${logObj.service}, Status: ${logObj.container_status}, Exit Code: ${logObj.exit_code}
Logs: ${logObj.logs.join('\n')}
Return ONLY a JSON object: { "rca": "2 sentence explanation", "command": "docker command to heal/restart" }`;
        
        try {
            const model = genAI.getGenerativeModel({ model: "gemini-2.5-flash" });
            const result = await model.generateContent(prompt);
            let text = (await result.response).text().trim();
            if (text.startsWith('\`\`\`json')) text = text.replace(/^\`\`\`json/, '').replace(/\`\`\`$/, '').trim();
            else if (text.startsWith('\`\`\`')) text = text.replace(/^\`\`\`/, '').replace(/\`\`\`$/, '').trim();
            const aiResult = JSON.parse(text);
            
            console.log(`============ AI ROOT CAUSE ANALYSIS ============`);
            console.log(`Impacted Service: ${logObj.service}`);
            console.log(`RCA: ${aiResult.rca}`);
            console.log(`Target Healing Command: ${aiResult.command}`);
            console.log(`================================================`);
            
            const rcaEvent = { service: logObj.service, rca: aiResult.rca, command: aiResult.command, timestamp: new Date().toISOString() };
            state.rcaEvents.unshift(rcaEvent);
            if(state.rcaEvents.length > 50) state.rcaEvents.pop();
            io.emit('rca_event', rcaEvent);
            sendEmailAlert(logObj.service, aiResult.rca, aiResult.command);
            if (aiResult.command) executeHealingSequence(aiResult.command, logObj.service);
        } catch (err) { 
            if (err.message.includes('429') || err.message.includes('quota') || err.message.includes('exceeded')) {
                 console.warn(`[RATE LIMIT OVERLOAD] Gemini API Free Tier Quota Exhausted. Autonomous Fallback Healing Engine activated for ${logObj.service}.`);
                 
                 const fallbackResult = {
                     rca: "Generative AI analysis unavailable due to daily API quota depletion. Triggering deterministic fallback restart sequence based on catastrophic container threshold heuristics.",
                     command: `docker restart ${logObj.service}`
                 };
                 
                 console.log(`============ FALLBACK ROOT CAUSE ANALYSIS ============`);
                 console.log(`Impacted Service: ${logObj.service}`);
                 console.log(`RCA: ${fallbackResult.rca}`);
                 console.log(`Target Healing Command: ${fallbackResult.command}`);
                 console.log(`======================================================`);
                 
                 const rcaEvent = { service: logObj.service, rca: fallbackResult.rca, command: fallbackResult.command, timestamp: new Date().toISOString() };
                 state.rcaEvents.unshift(rcaEvent);
                 if(state.rcaEvents.length > 50) state.rcaEvents.pop();
                 io.emit('rca_event', rcaEvent);
                 sendEmailAlert(logObj.service, fallbackResult.rca, fallbackResult.command);
                 executeHealingSequence(fallbackResult.command, logObj.service);
                 
                 // Apply extended 5-minute cooldown to prevent looping fallback sequences
                 coolingDown[logObj.service] = Date.now() + 300000;
            } else {
                 console.error(`AI Engine failed or parsing mismatched schema: ${err.message}`);
                 coolingDown[logObj.service] = 0; 
            }
        }
    }
}

connectRabbitMQ();
server.listen(4000, () => console.log('AI API Backend + Socket listening on *:4000'));
