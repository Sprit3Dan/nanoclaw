"""Tool to check remote status of delegated A2A tasks."""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool
from nanobot.bus.delegation import DelegationTaskMap


class DelegationRemoteStatusTool(Tool):
    """Fetch remote A2A task status for a locally tracked delegation task."""

    def __init__(self, delegation_map: DelegationTaskMap | None = None):
        self._delegation_map = delegation_map

    @property
    def name(self) -> str:
        return "delegation_remote_status"

    @property
    def description(self) -> str:
        return (
            "Check remote execution status for a delegated A2A task using "
            "the stored remote status URL in local delegation metadata."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Local delegation task id.",
                },
                "include_metadata": {
                    "type": "boolean",
                    "description": "Include local delegation metadata in the response payload.",
                },
            },
            "required": ["task_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        if not self._delegation_map:
            return "Error: delegation map is not configured."

        task_id_raw = kwargs.get("task_id")
        local_id = str(task_id_raw).strip() if task_id_raw is not None else ""
        if not local_id:
            return "Error: task_id is required."

        include_metadata_raw = kwargs.get("include_metadata", False)
        include_metadata = (
            include_metadata_raw
            if isinstance(include_metadata_raw, bool)
            else str(include_metadata_raw).strip().lower() in {"1", "true", "yes"}
        )

        task = self._delegation_map.get(local_id)
        if task is None:
            return f"Delegation task not found: {local_id}"

        broker_result = self._broker_status_snapshot(local_id)
        if broker_result is not None:
            remote_result = broker_result
        else:
            status_url = self._pick_remote_status_url(task)
            if not status_url:
                return "Error: no broker status event and remote status URL is missing in task metadata."

            timeout = self._timeout_from_env()
            remote_result = await self._fetch_remote_status(status_url, timeout=timeout)

        payload = {
            "local_task": self._serialize(task, include_metadata=True),
            "remote_check": remote_result,
        }
        if not include_metadata and "metadata" in payload["local_task"]:
            payload["local_task"].pop("metadata", None)

        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def _fetch_remote_status(self, url: str, *, timeout: float) -> dict[str, Any]:
        started = time.time()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=timeout)) as client:
                resp = await client.get(url)
        except Exception as exc:
            return {
                "ok": False,
                "status_url": url,
                "error": f"request_failed: {exc}",
                "duration_ms": int((time.time() - started) * 1000),
            }

        out: dict[str, Any] = {
            "ok": resp.status_code < 400,
            "status_code": resp.status_code,
            "status_url": url,
            "duration_ms": int((time.time() - started) * 1000),
        }

        try:
            out["body"] = resp.json()
        except Exception:
            out["body_text"] = (resp.text or "")[:2000]

        return out

    def _broker_status_snapshot(self, local_id: str) -> dict[str, Any] | None:
        if not self._delegation_map:
            return None

        task = self._delegation_map.get(local_id)
        if task is None:
            return None

        event = self._delegation_map.get_status_event(task.delegation_id)
        if not isinstance(event, dict):
            return None

        status = str(event.get("status", "")).strip().lower()
        payload = event.get("payload")
        body = payload if isinstance(payload, dict) else {"value": payload}
        return {
            "ok": status in {"completed", "done"},
            "source": "broker",
            "delegation_id": task.delegation_id,
            "status": status or "unknown",
            "from_agent": str(event.get("from_agent", "")).strip(),
            "updated_at": event.get("updated_at"),
            "body": body,
        }

    @classmethod
    def _timeout_from_env(cls) -> float:
        raw = os.environ.get("NANOBOT_DELEGATION_REMOTE_TIMEOUT_S", "10")
        return cls._coerce_timeout(raw)

    @staticmethod
    def _coerce_timeout(raw: Any) -> float:
        try:
            value = float(raw)
        except Exception:
            return 10.0
        if value < 1:
            return 1.0
        if value > 120:
            return 120.0
        return value

    @staticmethod
    def _pick_remote_status_url(task: Any) -> str | None:
        metadata = task.metadata if isinstance(getattr(task, "metadata", None), dict) else {}

        for key in ("remote_status_url", "a2a_remote_status_url"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        raw_delegation = metadata.get("_delegation")
        if isinstance(raw_delegation, dict):
            for key in ("remote_status_url", "a2a_remote_status_url"):
                value = raw_delegation.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        return None

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
            "delegation_id": task.delegation_id,
            "origin_channel": task.origin_channel,
            "origin_chat_id": task.origin_chat_id,
            "delegated_channel": task.delegated_channel,
            "delegated_task_id": task.delegated_task_id,
            "delegated_agent_id": task.delegated_agent_id,
        }
        if include_metadata:
            item["metadata"] = dict(task.metadata or {})
        return item

