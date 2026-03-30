/**
 * Internal mailer: POST /send-alert with JSON { recipients, rca_event }.
 * Requires X-Internal-Token matching MAILER_INTERNAL_TOKEN.
 *
 * Sending: Resend API (RESEND_API_KEY + RESEND_FROM) preferred; else Gmail via nodemailer.
 */
const express = require("express");
const nodemailer = require("nodemailer");

const PORT = parseInt(process.env.PORT || "4100", 10);
const INTERNAL_TOKEN = (process.env.MAILER_INTERNAL_TOKEN || "").trim();
const RESEND_API_KEY = (process.env.RESEND_API_KEY || "").trim();
const RESEND_FROM = (process.env.RESEND_FROM || "").trim();
const EMAIL_USER = process.env.EMAIL_USER || "";
const EMAIL_PASS = process.env.EMAIL_PASS || "";

let transporter = null;
function getTransporter() {
  if (!transporter && EMAIL_USER && EMAIL_PASS) {
    transporter = nodemailer.createTransport({
      service: "gmail",
      auth: { user: EMAIL_USER, pass: EMAIL_PASS },
    });
  }
  return transporter;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function buildBodies(rcaEvent) {
  const service = rcaEvent.service || "unknown";
  const rca = rcaEvent.rca || "";
  const command = rcaEvent.command || "";
  const timestamp = rcaEvent.timestamp || new Date().toISOString();
  const kind = rcaEvent.alert_kind || "failure";

  const escSvc = escapeHtml(service);
  const escCmd = escapeHtml(command);
  const escRca = escapeHtml(rca);
  const escTs = escapeHtml(timestamp);

  if (kind === "analysis_error") {
    const subject = `🚨 Alert: Analysis failed for ${service}`;
    const text = `AI analysis error: ${service}\n\nDetails:\n${rca}\n\nCommand line (not executed):\n${command}\n\nTime: ${timestamp}`;
    const html = `
    <div style="font-family: Inter, system-ui, sans-serif; max-width: 600px; margin: auto;">
      <div style="background: #92400e; color: white; padding: 20px 24px; border-radius: 8px 8px 0 0;">
        <h2 style="margin:0;">🚨 AI analysis failed: ${escSvc}</h2>
      </div>
      <div style="background: #f9fafb; padding: 24px; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 8px 8px;">
        <h3 style="color: #111827; margin-bottom: 8px;">Details</h3>
        <pre style="background: #1f2937; color: #fde68a; padding: 12px 16px; border-radius: 6px;
                    font-size: 12px; overflow-x: auto; white-space: pre-wrap;">${escRca}</pre>
        <h3 style="color: #111827; margin-top: 20px; margin-bottom: 8px;">Healing command</h3>
        <pre style="background: #1f2937; color: #a7f3d0; padding: 12px 16px; border-radius: 6px;
                    font-size: 13px; overflow-x: auto;">${escCmd}</pre>
        <p style="margin-top: 20px; font-size: 12px; color: #9ca3af;">Time: ${escTs}</p>
      </div>
    </div>`;
    return { subject, text, html };
  }

  if (kind === "escalation") {
    const subject = `🚨 Human escalation required: ${service}`;
    const text = `Human escalation: ${service}\n\nNo automated Docker command was executed. Review the details below.\n\nContext:\n${rca}\n\nManual follow-up (placeholder):\n${command}\n\nTime: ${timestamp}`;
    const html = `
    <div style="font-family: Inter, system-ui, sans-serif; max-width: 600px; margin: auto;">
      <div style="background: #7c2d12; color: white; padding: 20px 24px; border-radius: 8px 8px 0 0;">
        <h2 style="margin:0;">🚨 Human escalation: ${escSvc}</h2>
      </div>
      <div style="background: #f9fafb; padding: 24px; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 8px 8px;">
        <p style="color: #374151; margin-bottom: 16px;">No automated container action was run. An engineer should investigate.</p>
        <h3 style="color: #111827; margin-bottom: 8px;">Context</h3>
        <p style="color: #374151; line-height: 1.6; white-space: pre-wrap;">${escRca}</p>
        <h3 style="color: #111827; margin-top: 20px; margin-bottom: 8px;">Manual follow-up</h3>
        <pre style="background: #1f2937; color: #fde68a; padding: 12px 16px; border-radius: 6px;
                    font-size: 13px; overflow-x: auto; white-space: pre-wrap;">${escCmd}</pre>
        <p style="margin-top: 20px; font-size: 12px; color: #9ca3af;">${escTs}</p>
      </div>
    </div>`;
    return { subject, text, html };
  }

  const subject = `🚨 Alert: Failure detected in ${service}`;
  const text = `Microservice Alert: ${service}\n\nRoot Cause Analysis:\n${rca}\n\nHealing Command Executed:\n${command}\n\nTime: ${timestamp}`;
  const html = `
    <div style="font-family: Inter, system-ui, sans-serif; max-width: 600px; margin: auto;">
      <div style="background: #1e5631; color: white; padding: 20px 24px; border-radius: 8px 8px 0 0;">
        <h2 style="margin:0;">🚨 Microservice Alert: ${escSvc}</h2>
      </div>
      <div style="background: #f9fafb; padding: 24px; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 8px 8px;">
        <h3 style="color: #111827; margin-bottom: 8px;">Root Cause Analysis</h3>
        <p style="color: #374151; line-height: 1.6; white-space: pre-wrap;">${escRca}</p>
        <h3 style="color: #111827; margin-top: 20px; margin-bottom: 8px;">Healing Command Executed</h3>
        <pre style="background: #1f2937; color: #a7f3d0; padding: 12px 16px; border-radius: 6px;
                    font-size: 13px; overflow-x: auto;">${escCmd}</pre>
        <p style="margin-top: 20px; font-size: 12px; color: #9ca3af;">Detected at: ${escTs}</p>
      </div>
    </div>`;
  return { subject, text, html };
}

/**
 * @returns {{ id: string }}
 */
async function sendViaResend(recipients, subject, html, text) {
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: RESEND_FROM,
      to: recipients,
      subject,
      html,
      text,
    }),
  });
  const bodyText = await res.text();
  let data;
  try {
    data = JSON.parse(bodyText);
  } catch {
    data = { raw: bodyText };
  }
  if (!res.ok) {
    const msg = data.message || data.name || bodyText || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return { id: data.id || String(data) };
}

const app = express();
app.use(express.json({ limit: "512kb" }));

app.get("/health", (_req, res) => {
  res.json({ ok: true });
});

app.post("/send-alert", async (req, res) => {
  const token = String(req.headers["x-internal-token"] || "").trim();
  if (!INTERNAL_TOKEN || token !== INTERNAL_TOKEN) {
    return res.status(401).json({ error: "unauthorized" });
  }

  const useResend = Boolean(RESEND_API_KEY && RESEND_FROM);
  const useGmail = Boolean(EMAIL_USER && EMAIL_PASS);
  if (!useResend && !useGmail) {
    return res.status(503).json({
      error: "email_not_configured",
      hint: "Set RESEND_API_KEY and RESEND_FROM (recommended), or EMAIL_USER and EMAIL_PASS for Gmail SMTP",
    });
  }

  const { recipients, rca_event: rcaEvent } = req.body || {};
  if (!Array.isArray(recipients) || recipients.length === 0) {
    return res.status(400).json({ error: "recipients_required" });
  }
  if (!rcaEvent || typeof rcaEvent !== "object") {
    return res.status(400).json({ error: "rca_event_required" });
  }

  const { subject, text, html } = buildBodies(rcaEvent);

  try {
    if (useResend) {
      const out = await sendViaResend(recipients, subject, html, text);
      console.log(`[mail-service] Resend id=${out.id} to=${recipients.join(",")}`);
      return res.json({ ok: true, provider: "resend", messageId: out.id });
    }
    const tx = getTransporter();
    const info = await tx.sendMail({
      from: EMAIL_USER,
      to: recipients.join(", "),
      subject,
      text,
      html,
    });
    console.log(`[mail-service] Gmail messageId=${info.messageId} to=${recipients.join(",")}`);
    return res.json({ ok: true, provider: "gmail", messageId: info.messageId });
  } catch (err) {
    const msg = err.message || String(err);
    console.error("[mail-service] send failed:", msg);
    const payload = { error: "send_failed", message: msg };
    if (
      msg.includes("only send testing emails") ||
      msg.includes("verify a domain")
    ) {
      payload.hint =
        "Resend test sender: you can only deliver to your Resend account email. " +
        "Use that address as the dashboard/EMAIL_ALERT_RECIPIENTS recipient, or verify a domain at https://resend.com/domains and set RESEND_FROM to an address on that domain to mail anyone.";
    }
    return res.status(502).json(payload);
  }
});

app.listen(PORT, "0.0.0.0", () => {
  const mode =
    RESEND_API_KEY && RESEND_FROM
      ? "resend"
      : EMAIL_USER && EMAIL_PASS
        ? "gmail"
        : "no-sender";
  console.log(`mail-service listening on :${PORT} (mode=${mode})`);
});
