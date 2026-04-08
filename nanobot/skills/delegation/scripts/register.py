#!/usr/bin/env python3
"""Register this agent's A2A capabilities with the discovery service.

Scans workspace SKILL.md files for skills that have an ``a2a`` block in their
nanobot frontmatter metadata, builds a capabilities payload, and POSTs it to
the discovery service.  Uses only the standard library — runs in any Python 3.8+
environment without installing additional packages.

Required env vars:
    NANOBOT_DISCOVERY_BASE_URL   Base URL of the A2A discovery service
    NANOBOT_A2A_AGENT_ID         Agent identifier (e.g. "aircraft")

Optional env vars:
    NANOBOT_WORKSPACE                Workspace directory (default: ~/.nanobot/workspace)
    NANOBOT_A2A_ADVERTISED_BASE_URL  Externally reachable base URL for this agent's A2A server
    NANOBOT_A2A_LISTEN_PORT          A2A listen port (default: 19100)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse simple key: value YAML frontmatter from a markdown file."""
    if not text.startswith("---"):
        return {}
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    result: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip().strip("\"'")
    return result


def _nanobot_meta(frontmatter: dict[str, str]) -> dict:
    raw = frontmatter.get("metadata", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data.get("nanobot", data.get("openclaw", {}))
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


# ---------------------------------------------------------------------------
# Skill discovery
# ---------------------------------------------------------------------------

def _collect_skills(workspace: Path) -> list[dict]:
    """Return A2A skill entries from workspace SKILL.md files that have an a2a block."""
    skills_dir = workspace / "skills"
    if not skills_dir.exists():
        return []

    skills = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue

        fm = _parse_frontmatter(text)
        nb = _nanobot_meta(fm)
        a2a = nb.get("a2a")
        if not a2a:
            continue  # opt-in only

        skills.append({
            "id": skill_dir.name,
            "name": skill_dir.name,
            "description": fm.get("description", skill_dir.name),
            "tags": a2a.get("tags", []),
            "examples": a2a.get("examples", []),
        })

    return skills


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def main() -> int:
    discovery_base = os.environ.get("NANOBOT_DISCOVERY_BASE_URL", "").strip().rstrip("/")
    if not discovery_base:
        print("ERROR: NANOBOT_DISCOVERY_BASE_URL is not set", file=sys.stderr)
        return 1

    agent_id = os.environ.get("NANOBOT_A2A_AGENT_ID", "agent").strip()
    listen_port = int(os.environ.get("NANOBOT_A2A_LISTEN_PORT", "19100"))
    advertised = os.environ.get("NANOBOT_A2A_ADVERTISED_BASE_URL", "").strip() or None

    workspace_env = os.environ.get("NANOBOT_WORKSPACE", "").strip()
    workspace = Path(workspace_env).expanduser() if workspace_env else Path.home() / ".nanobot" / "workspace"

    skills = _collect_skills(workspace)

    payload = {
        "agent_id": agent_id,
        "transport": "a2a-http",
        "protocol": "nanobot-a2a/v1",
        "address": {
            "port": listen_port,
            "base_url": advertised,
        },
        "capabilities": {
            "a2a_modes": ["push", "async", "sse"],
            "default_mode": "push",
            "semantic_routing": "discovery-knn",
            "skills": skills,
        },
    }

    url = f"{discovery_base}/register"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        embedded = result.get("embedded", "?")
        print(f"ok: agent_id={agent_id} skills={len(skills)} embedded={embedded} discovery={discovery_base}")
        return 0
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:300]
        print(f"ERROR: registration failed [{e.code}]: {body_text}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
