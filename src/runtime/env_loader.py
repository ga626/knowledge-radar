"""Small runtime env loader for standalone tools and adapters."""

from __future__ import annotations

import os


SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(SRC_ROOT)


def load_runtime_env() -> str:
    """Load the single product runtime .env file, returning its path or empty string."""
    candidates = [os.path.join(REPO_ROOT, ".env")]
    loaded: list[str] = []
    seen: set[str] = set()
    for env_path in candidates:
        if not env_path or env_path in seen or not os.path.isfile(env_path):
            continue
        seen.add(env_path)
        with open(env_path, "r", encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value and not os.environ.get(key):
                    os.environ[key] = value
        loaded.append(env_path)
    return loaded[0] if loaded else ""
