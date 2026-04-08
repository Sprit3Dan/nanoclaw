"""Tool to fetch persisted delegation result content by task_id or delegation_id."""

from __future__ import annotations

import json
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.bus.delegation import DelegationTaskMap


class DelegationResultGetTool(Tool):
    """Load persisted delegation result snapshots from local storage."""

    def __init__(self, delegation_map: DelegationTaskMap | None = None):
        self._delegation_map = delegation_map

    @property
    def name(self) -> str:
        return "delegation_result_get"

    @property
    def description(self) -> str:
        return (
            "Fetch full persisted delegation result content using either local task_id "
            "or canonical delegation_id."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Local delegation task id (as shown by delegation_tasks).",
                },
                "delegation_id": {
                    "type": "string",
                    "description": "Canonical delegation id.",
                },
                "include_task_metadata": {
                    "type": "boolean",
                    "description": "Include local task metadata when task_id is provided.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        if not self._delegation_map:
            return "Error: delegation map is not configured."

        include_meta_raw = kwargs.get("include_task_metadata", False)
        include_task_metadata = (
            include_meta_raw
            if isinstance(include_meta_raw, bool)
            else str(include_meta_raw).strip().lower() in {"1", "true", "yes"}
        )

        task_id_raw = kwargs.get("task_id")
        delegation_id_raw = kwargs.get("delegation_id")

        task_id = str(task_id_raw).strip() if task_id_raw is not None else ""
        delegation_id = str(delegation_id_raw).strip() if delegation_id_raw is not None else ""

        task = None
        if task_id:
            task = self._delegation_map.get(task_id)
            if task is None:
                return f"Delegation task not found: {task_id}"
            if not delegation_id:
                delegation_id = task.delegation_id

        if not delegation_id:
            return "Error: provide either task_id or delegation_id."

        result = self._delegation_map.load_result(delegation_id)
        status_event = self._delegation_map.get_status_event(delegation_id)

        payload: dict[str, Any] = {
            "delegation_id": delegation_id,
            "has_result": result is not None,
            "result": result,
            "status_event": status_event,
        }

        if task is not None:
            payload["task"] = {
                "id": task.id,
                "status": task.status,
                "delegation_id": task.delegation_id,
            }
            if include_task_metadata:
                payload["task"]["metadata"] = dict(task.metadata or {})

        if result is None:
            payload["hint"] = (
                "No persisted result found yet for this delegation_id. "
                "Try again after completion or inspect delegation_remote_status."
            )

        return json.dumps(payload, ensure_ascii=False, indent=2)