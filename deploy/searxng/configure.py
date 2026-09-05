"""Inject the Railway secret at startup, never into the image or logs."""
import json
import os
from pathlib import Path
import sys


def render(template, key):
    if not key or not key.strip():
        raise ValueError("BRAVE_SEARCH_API_KEY must be configured on the SearXNG service")
    return template.replace('"__BRAVE_SEARCH_API_KEY__"', json.dumps(key.strip()))


if __name__ == "__main__":
    template = Path("/usr/local/searxng/app-settings.yml").read_text()
    rendered = render(template, os.environ.get("BRAVE_SEARCH_API_KEY", ""))
    target = Path(os.environ.get("SEARXNG_SETTINGS_PATH", "/etc/searxng/settings.yml"))
    target.write_text(rendered)
    target.chmod(0o600)
    os.execv("/usr/local/searxng/entrypoint.sh", ["/usr/local/searxng/entrypoint.sh", *sys.argv[1:]])
