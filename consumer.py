# consumer.py — RabbitMQ → ai_engine integration bridge
#
# Integration only: consumes log events published by log-collector and feeds
# them into ai_engine.Agent.run(). No AI logic lives here.
#
# Environment variables (see .env.example):
#   RABBITMQ_URL      amqp://guest:guest@rabbitmq:5672/
#   RABBITMQ_QUEUE    logs_queue
#   DRY_RUN           true|false   (passed to ToolManager)
#   DISABLE_CONSUMER  true|false   (exit cleanly without starting)
#   LLM_PROVIDER      vertex_ai|gemini|openai|local
#   LLM_FALLBACKS     comma-separated list, e.g. gemini,openai

import json
import logging
import os
import time

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("consumer")

# Build RABBITMQ_URL from individual parts if the full URL is not provided.
# This supports both RABBITMQ_URL (compose style) and RABBITMQ_HOST/PORT/USER/PASS
# (the format used in the parent .env).
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
# Accept both DRY_RUN and TOOLS_DRY_RUN (alias used in some .env configurations)
DRY_RUN          = (os.getenv("DRY_RUN") or os.getenv("TOOLS_DRY_RUN", "false")).lower() == "true"
DISABLE_CONSUMER = os.getenv("DISABLE_CONSUMER", "false").lower() == "true"

RETRY_DELAY_SECONDS  = 5
RETRY_DELAY_MAX      = 30


def _build_agent():
    """Initialise StateManager, ToolManager and Agent from env configuration."""
    from ai_engine.state import StateManager
    from ai_engine.tools import ToolManager
    from ai_engine.agent import Agent

    provider  = os.getenv("LLM_PROVIDER", "gemini")
    fallbacks = [
        p.strip()
        for p in os.getenv("LLM_FALLBACKS", "openai").split(",")
        if p.strip()
    ]

    sm    = StateManager()
    tm    = ToolManager(sm, dry_run=DRY_RUN)
    agent = Agent(sm, tm, llm_provider=provider, fallback_providers=fallbacks)
    logger.info(
        "Agent ready (provider=%s, dry_run=%s)", agent.active_provider, DRY_RUN
    )
    return agent


def _on_message(channel, method, _properties, body, agent):
    """Callback invoked for every message delivered from RabbitMQ."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Received non-JSON message — skipping")
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    service          = data.get("service", "unknown")
    log_lines        = data.get("logs", [])
    container_status = data.get("container_status", "unknown")
    exit_code        = int(data.get("exit_code") or 0)
    timestamp        = data.get("timestamp")

    if not log_lines:
        logger.debug("Empty log payload for %s — skipping", service)
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    logger.info(
        "Received event: service=%s status=%s exit_code=%d lines=%d",
        service, container_status, exit_code, len(log_lines),
    )

    try:
        result = agent.run(
            service=service,
            log_lines=log_lines,
            container_status=container_status,
            exit_code=exit_code,
            timestamp=timestamp,
        )
        logger.info(
            "Incident %s closed: failure_type=%s action=%s healed=%s status=%s",
            result.get("incident_id"),
            result.get("failure_type"),
            result.get("decision", {}).get("action"),
            result.get("healed"),
            result.get("final_status"),
        )
    except Exception as exc:
        logger.error("Agent error processing event for %s: %s", service, exc)

    channel.basic_ack(delivery_tag=method.delivery_tag)


def _start_consuming(agent):
    """Connect to RabbitMQ and block on message consumption. Retries on failure."""
    import pika

    delay = RETRY_DELAY_SECONDS
    while True:
        try:
            params = pika.URLParameters(RABBITMQ_URL)
            conn   = pika.BlockingConnection(params)
            ch     = conn.channel()
            ch.queue_declare(queue=RABBITMQ_QUEUE, passive=True)
            ch.basic_qos(prefetch_count=1)
            ch.basic_consume(
                queue=RABBITMQ_QUEUE,
                on_message_callback=lambda ch, m, p, b: _on_message(ch, m, p, b, agent),
            )
            logger.info(
                "Connected to RabbitMQ at %s — consuming from queue '%s'",
                RABBITMQ_URL, RABBITMQ_QUEUE,
            )
            delay = RETRY_DELAY_SECONDS  # reset backoff on successful connection
            ch.start_consuming()
        except KeyboardInterrupt:
            logger.info("Shutdown requested — stopping consumer")
            break
        except Exception as exc:
            logger.error(
                "RabbitMQ connection lost: %s — retrying in %ds", exc, delay
            )
            time.sleep(delay)
            delay = min(delay * 2, RETRY_DELAY_MAX)


if __name__ == "__main__":
    if DISABLE_CONSUMER:
        logger.info("DISABLE_CONSUMER=true — exiting without starting")
        raise SystemExit(0)

    logger.info("Starting AI Engine consumer (dry_run=%s)", DRY_RUN)
    agent = _build_agent()
    _start_consuming(agent)
