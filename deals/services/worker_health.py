"""Tiny Railway health server plus Redis worker-presence heartbeat."""
from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from django.conf import settings
from django.core.cache import cache


def record_presence(*, role: str, instance_id: str, started_at: float) -> dict:
    """Publish presence without allowing an older overlapping deploy to win."""
    presence = {
        "role": role,
        "instance_id": instance_id,
        "celery_node": socket.gethostname(),
        "started_at": started_at,
        "heartbeat_at": time.time(),
    }
    ttl = int(getattr(settings, "VDR_WORKER_HEARTBEAT_TTL", 90))
    cache.set(f"vdr:presence:{role}:{instance_id}", presence, timeout=ttl)
    current_key = f"vdr:presence:{role}:current"
    current = cache.get(current_key)
    if not isinstance(current, dict) or float(current.get("started_at") or 0) <= started_at:
        cache.set(current_key, presence, timeout=ttl)
    return presence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("worker", "coordinator"), required=True)
    args = parser.parse_args()
    instance_id = os.getenv("RAILWAY_DEPLOYMENT_ID") or f"{args.role}:{socket.gethostname()}"
    started_at = time.time()

    def pulse():
        while True:
            record_presence(role=args.role, instance_id=instance_id, started_at=started_at)
            time.sleep(int(getattr(settings, "VDR_WORKER_HEARTBEAT_SECONDS", 30)))

    threading.Thread(target=pulse, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.rstrip("/") == "/api/core/health":
                body = json.dumps({"status": "ok", "service": args.role}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            return

    HTTPServer(("0.0.0.0", int(os.getenv("PORT", "8000"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
