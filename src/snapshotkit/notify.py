"""Optional failure notification via webhook. Never raises."""
import json
import sys
import urllib.request


def send_failure(webhook_url: str | None, payload: dict) -> None:
    if not webhook_url:
        return
    try:
        body = json.dumps(
            {
                k: payload.get(k)
                for k in ("project", "image_digest", "reason", "pipeline_run_url")
            }
        ).encode()
        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as e:  # noqa: BLE001 - notification must never fail the build
        print(f"snapshot: failure notification not delivered: {type(e).__name__}", file=sys.stderr)
