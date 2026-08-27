#!/usr/bin/env python3
"""Lightweight heartbeat watcher for the monitoring stack.

Polls the health endpoint of each configured service and sends a Telegram
notification when a service has been unavailable for longer than the configured
threshold. A recovery notification is sent once the service is healthy again.

The watcher is intentionally independent of Prometheus/Alertmanager so that it
can still notify when those components themselves are down.

Configuration is provided entirely through environment variables:
  HEARTBEAT_TARGETS            JSON list of {"name": ..., "url": ...} objects
  HEARTBEAT_INTERVAL           Seconds between poll cycles (default 30)
  HEARTBEAT_FAILURE_THRESHOLD  Seconds a service must be down before alerting
                               (default 300 = 5 minutes)
  HEARTBEAT_TIMEOUT            Per-request timeout in seconds (default 5)
  HEARTBEAT_HOSTNAME           Optional label included in messages
  TELEGRAM_BOT_TOKEN           Telegram bot token
  TELEGRAM_CHAT_ID             Telegram chat id
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


TARGETS = json.loads(os.environ.get("HEARTBEAT_TARGETS", "[]"))
INTERVAL = env_int("HEARTBEAT_INTERVAL", 30)
THRESHOLD = env_int("HEARTBEAT_FAILURE_THRESHOLD", 300)
TIMEOUT = env_int("HEARTBEAT_TIMEOUT", 5)
# Log the result of every individual check each cycle, not just state changes.
LOG_CHECKS = os.environ.get("HEARTBEAT_LOG_CHECKS", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
HOSTNAME = os.environ.get("HEARTBEAT_HOSTNAME", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Optional dead-man's-switch: pinged every cycle so an external service can
# alert if this watcher (or the whole host) stops reporting in.
PING_URL = os.environ.get("HEARTBEAT_PING_URL", "")


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(stamp + " " + message, flush=True)


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        log("telegram not configured, skipping notification: " + text)
        return
    url = "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    ).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001 - never let notify crash the loop
        log("failed to send telegram notification: " + repr(exc))


def ping_dead_mans_switch():
    if not PING_URL:
        return
    try:
        with urllib.request.urlopen(PING_URL, timeout=TIMEOUT) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001 - never let a ping failure crash the loop
        log("failed to ping dead-man's-switch: " + repr(exc))


def check(url):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 400
    except Exception:  # noqa: BLE001 - any failure means "unhealthy"
        return False


def host_label():
    return (" on <b>" + HOSTNAME + "</b>") if HOSTNAME else ""


def human_duration(seconds):
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    if minutes:
        return str(minutes) + "m" + str(seconds) + "s"
    return str(seconds) + "s"


def main():
    if not TARGETS:
        log("no HEARTBEAT_TARGETS configured, nothing to watch")
        return
    log(
        "heartbeat started: interval=%ds threshold=%ds targets=%s"
        % (INTERVAL, THRESHOLD, ", ".join(t["name"] for t in TARGETS))
    )

    # Per-service state: when it first went down and whether we alerted already.
    state = {t["name"]: {"down_since": None, "alerted": False} for t in TARGETS}

    while True:
        now = time.monotonic()
        for target in TARGETS:
            name = target["name"]
            st = state[name]
            healthy = check(target["url"])

            if LOG_CHECKS:
                log("check %s: %s" % (name, "healthy" if healthy else "UNHEALTHY"))

            if healthy:
                if st["alerted"]:
                    downtime = human_duration(now - st["down_since"])
                    send_telegram(
                        "✅ <b>%s</b> recovered%s (was down for %s)"
                        % (name, host_label(), downtime)
                    )
                    log(name + " recovered after " + downtime)
                st["down_since"] = None
                st["alerted"] = False
                continue

            # Unhealthy.
            if st["down_since"] is None:
                st["down_since"] = now
                log(name + " is unavailable, starting failure timer")
            down_for = now - st["down_since"]
            if not st["alerted"] and down_for >= THRESHOLD:
                send_telegram(
                    "\U0001F534 <b>%s</b> is unavailable%s for %s"
                    % (name, host_label(), human_duration(down_for))
                )
                st["alerted"] = True
                log(name + " alert sent after " + human_duration(down_for))

        # Signal liveness of the watcher/host to the external dead-man's-switch.
        ping_dead_mans_switch()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
