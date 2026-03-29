"""Tool to inspect pending delegation tasks from the delegation map."""

from __future__ import annotations

import json
import time
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.bus.delegation import DelegationTaskMap


class DelegationTasksTool(Tool):
    """List and inspect delegation tasks tracked in the in-memory map."""

    def __init__(self, delegation_map: DelegationTaskMap | None = None):
        self._delegation_map = delegation_map

    @property
    def name(self) -> str:
        return "delegation_tasks"

    @property
    def description(self) -> str:
        return (
            "List pending delegation tasks correlated by correlation_id. "
            "Use this to inspect active delegated conversations."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_pending", "get"],
                    "description": "list_pending = list active tasks, get = inspect one task by local delegation id.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Local delegation task id (correlation-tracked; required for action=get).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum number of tasks to return for list_pending.",
                },
                "older_than_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional age filter (created at least this many seconds ago).",
                },
                "include_metadata": {
                    "type": "boolean",
                    "description": "Include stored task metadata in output.",
                },
            },
        }

    async def execute(
        self,
        action: str = "list_pending",
        task_id: str | None = None,
        limit: int = 50,
        older_than_seconds: int | None = None,
        include_metadata: bool = False,
        **_: Any,
    ) -> str:
        if not self._delegation_map:
            return "Error: delegation map is not configured."

        if action == "get":
            local_id = str(task_id or "").strip()
            if not local_id:
                return "Error: task_id is required for action=get."
            task = self._delegation_map.get(local_id)
            if task is None:
                return f"Delegation task not found: {local_id}"
            return json.dumps(self._serialize(task, include_metadata), ensure_ascii=False, indent=2)

        if action != "list_pending":
            return "Error: action must be one of list_pending|get."

        now = time.time()
        rows = self._delegation_map.list_active()
        if older_than_seconds is not None:
            cutoff = now - max(1, int(older_than_seconds))
            rows = [t for t in rows if t.created_at <= cutoff]

        rows.sort(key=lambda t: t.created_at)
        rows = rows[: max(1, min(int(limit), 200))]

        if not rows:
            return "No pending delegation tasks."

        payload = {
            "count": len(rows),
            "pending": [self._serialize(task, include_metadata) for task in rows],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _serialize(task: Any, include_metadata: bool) -> dict[str, Any]:
        item = {
            "id": task.id,
            "status": task.status,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "age_seconds": max(0, int(time.time() - task.created_at)),
            "reply_channel": task.reply_channel,
            "reply_chat_id": task.reply_chat_id,
            "correlation_id": task.correlation_id,
            "origin_channel": task.origin_channel,
            "origin_chat_id": task.origin_chat_id,
            "delegated_channel": task.delegated_channel,
            "delegated_task_id": task.delegated_task_id,
            "delegated_agent_id": task.delegated_agent_id,
        }
        if include_metadata:
            item["metadata"] = dict(task.metadata or {})
        return item
