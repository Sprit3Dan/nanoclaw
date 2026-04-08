"""Delegation task map abstraction for routing delegated replies."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from loguru import logger


@dataclass(slots=True)
class DelegationTask:
    """Represents a single delegation lifecycle."""

    id: str
    created_at: float
    updated_at: float

    # Where the final reply must be delivered.
    reply_channel: str
    reply_chat_id: str

    # Delegation contract details.
    delegation_id: str

    # Optional source/origin context that created this delegation.
    origin_channel: str = ""
    origin_chat_id: str = ""

    delegated_channel: str = "a2a"
    delegated_task_id: str | None = None
    delegated_agent_id: str | None = None

    # Lifecycle state.
    status: str = "created"  # created | dispatched | completed | expired | cancelled
    completed_at: float | None = None

    # Optional free-form metadata for integration/debugging.
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_active(self) -> bool:
        return self.status in {"created", "dispatched"}


class DelegationTaskMap:
    """
    In-memory hashmap/index for delegated conversations.

    This is key-based delegation-id storage, not FIFO queue semantics.
    It supports multiple concurrent A2A delegations and out-of-order replies:
    if a matching delegation id is present, it is resolved and routed.

    Typical flow:
    1) create() with a required delegation_id (capture reply target)
    2) bind_remote_task_id() after outbound delegation is accepted
    3) resolve_reply_target() on inbound A2A response
    4) mark_completed() after forwarding response to the customer channel
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._tasks: dict[str, DelegationTask] = {}
        self._by_delegation_id: dict[str, str] = {}
        self._status_by_delegation_id: dict[str, dict[str, Any]] = {}
        raw_audit_path = os.environ.get("NANOBOT_DELEGATION_AUDIT_JSONL", "memory/delegation_tasks.jsonl")
        self._audit_path = Path(raw_audit_path).expanduser()

    def create(
        self,
        *,
        reply_channel: str,
        reply_chat_id: str,
        delegation_id: str,
        origin_channel: str = "",
        origin_chat_id: str = "",
        delegated_channel: str = "a2a",
        metadata: Mapping[str, Any] | None = None,
    ) -> DelegationTask:
        now = time.time()
        delegation = str(delegation_id).strip()
        if not delegation:
            raise ValueError("delegation_id is required")

        task = DelegationTask(
            id=self._new_id(),
            created_at=now,
            updated_at=now,
            reply_channel=str(reply_channel).strip(),
            reply_chat_id=str(reply_chat_id).strip(),
            delegation_id=delegation,
            origin_channel=str(origin_channel).strip(),
            origin_chat_id=str(origin_chat_id).strip(),
            delegated_channel=str(delegated_channel).strip() or "a2a",
            metadata=dict(metadata or {}),
        )

        with self._lock:
            self._tasks[task.id] = task
            self._by_delegation_id[task.delegation_id] = task.id

        logger.debug(
            "delegation.create local_id={} delegation_id={} reply={}#{} origin={}#{} delegated_channel={}",
            task.id,
            task.delegation_id,
            task.reply_channel,
            task.reply_chat_id,
            task.origin_channel,
            task.origin_chat_id,
            task.delegated_channel,
        )
        self._append_audit_event(
            "create",
            task,
            extra={
                "reply_channel": task.reply_channel,
                "reply_chat_id": task.reply_chat_id,
                "origin_channel": task.origin_channel,
                "origin_chat_id": task.origin_chat_id,
                "delegated_channel": task.delegated_channel,
                "delegation_id": task.delegation_id,
            },
        )
        return task

    def get(self, delegation_task_id: str) -> DelegationTask | None:
        with self._lock:
            return self._tasks.get(str(delegation_task_id).strip())

    def bind_remote_task_id(
        self,
        *,
        delegation_id: str,
        delegated_task_id: str,
        delegated_agent_id: str | None = None,
    ) -> bool:
        """Attach downstream remote task details to an existing delegation-id tracked delegation task."""
        delegation = str(delegation_id).strip()
        remote_id = str(delegated_task_id).strip()
        if not delegation or not remote_id:
            logger.debug(
                "delegation.bind_remote skipped invalid values delegation_id='{}' remote_id='{}'",
                delegation,
                remote_id,
            )
            return False

        with self._lock:
            local_id = self._by_delegation_id.get(delegation)
            if not local_id:
                logger.debug("delegation.bind_remote skipped delegation_id={} reason=not_found", delegation)
                return False

            task = self._tasks.get(local_id)
            if task is None or not task.is_active():
                logger.debug(
                    "delegation.bind_remote skipped local_id={} reason={}",
                    local_id,
                    "not_found" if task is None else f"inactive:{task.status}",
                )
                return False

            task.delegated_task_id = remote_id
            if delegated_agent_id:
                task.delegated_agent_id = str(delegated_agent_id).strip() or None
            task.status = "dispatched"
            task.updated_at = time.time()

            logger.debug(
                "delegation.bind_remote local_id={} delegation_id={} remote_id={} remote_agent_id={} status={}",
                task.id,
                task.delegation_id,
                remote_id,
                task.delegated_agent_id,
                task.status,
            )
            self._append_audit_event(
                "bind_remote_task_id",
                task,
                extra={
                    "delegation_id": task.delegation_id,
                    "delegated_task_id": remote_id,
                    "delegated_agent_id": task.delegated_agent_id,
                },
            )
            return True

    def resolve(self, metadata: Mapping[str, Any] | None) -> DelegationTask | None:
        """
        Resolve active delegation task from inbound metadata.

        Lookup is delegation-id based and order-independent, so out-of-order A2A replies
        are handled correctly whenever a matching delegation id is available.
        Supports delegation_id across top-level, `_a2a`, and `_delegation` metadata.
        """
        candidates = self._extract_delegation_id_candidates(metadata)
        if not candidates:
            logger.debug("delegation.resolve no delegation-id candidates in metadata")
            return None

        with self._lock:
            for delegation_id in candidates:
                local_id = self._by_delegation_id.get(delegation_id)
                if not local_id:
                    continue
                task = self._tasks.get(local_id)
                if task and task.is_active():
                    logger.debug(
                        "delegation.resolve matched delegation_id={} -> local_id={} status={}",
                        delegation_id,
                        task.id,
                        task.status,
                    )
                    return task

        logger.debug("delegation.resolve no active match candidates={}", candidates)
        return None

    def resolve_reply_target(self, metadata: Mapping[str, Any] | None) -> tuple[str, str] | None:
        """
        Return (channel, chat_id) where delegated reply should be sent.
        """
        task = self.resolve(metadata)
        if task is None:
            return None
        return task.reply_channel, task.reply_chat_id

    def record_status_event(
        self,
        delegation_id: str,
        *,
        status: str,
        from_agent: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        """Store latest broker status event for a delegation_id."""
        did = str(delegation_id).strip()
        st = str(status).strip()
        if not did or not st:
            return False

        event: dict[str, Any] = {
            "delegation_id": did,
            "status": st,
            "from_agent": (str(from_agent).strip() if isinstance(from_agent, str) else ""),
            "updated_at": time.time(),
            "payload": dict(payload or {}),
        }

        with self._lock:
            self._status_by_delegation_id[did] = event

        logger.debug(
            "delegation.status.record delegation_id={} status={} from_agent={}",
            did,
            st,
            event["from_agent"],
        )
        return True

    def get_status_event(self, delegation_id: str) -> dict[str, Any] | None:
        """Return latest broker status event by delegation_id."""
        did = str(delegation_id).strip()
        if not did:
            return None
        with self._lock:
            event = self._status_by_delegation_id.get(did)
            return dict(event) if isinstance(event, dict) else None

    def mark_completed(self, delegation_task_id: str) -> bool:
        local_id = str(delegation_task_id).strip()
        if not local_id:
            logger.debug("delegation.complete skipped empty local_id")
            return False

        with self._lock:
            task = self._tasks.get(local_id)
            if task is None or not task.is_active():
                logger.debug(
                    "delegation.complete skipped local_id={} reason={}",
                    local_id,
                    "not_found" if task is None else f"inactive:{task.status}",
                )
                return False
            task.status = "completed"
            task.completed_at = time.time()
            task.updated_at = task.completed_at
            self._by_delegation_id.pop(task.delegation_id, None)
            self._status_by_delegation_id.pop(task.delegation_id, None)
            logger.debug(
                "delegation.complete local_id={} remote_id={} completed_at={}",
                task.id,
                task.delegated_task_id,
                task.completed_at,
            )
            self._append_audit_event(
                "mark_completed",
                task,
                extra={"completed_at": task.completed_at},
            )
            return True

    def expire(self, *, older_than_seconds: int) -> int:
        """Expire active tasks older than a given age. Returns expired count."""
        ttl = max(1, int(older_than_seconds))
        cutoff = time.time() - ttl
        expired = 0

        with self._lock:
            for task in self._tasks.values():
                if not task.is_active():
                    continue
                if task.updated_at > cutoff:
                    continue
                task.status = "expired"
                task.updated_at = time.time()
                self._by_delegation_id.pop(task.delegation_id, None)
                self._status_by_delegation_id.pop(task.delegation_id, None)
                expired += 1
                logger.debug(
                    "delegation.expire local_id={} remote_id={} ttl_s={}",
                    task.id,
                    task.delegated_task_id,
                    ttl,
                )
                self._append_audit_event(
                    "expire",
                    task,
                    extra={"ttl_seconds": ttl},
                )

        logger.debug("delegation.expire summary ttl_s={} expired={}", ttl, expired)
        return expired

    def list_active(self) -> list[DelegationTask]:
        with self._lock:
            return [task for task in self._tasks.values() if task.is_active()]

    def _append_audit_event(
        self,
        event: str,
        task: DelegationTask,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "delegation_task_id": task.id,
            "status": task.status,
            "reply_channel": task.reply_channel,
            "reply_chat_id": task.reply_chat_id,
            "delegation_id": task.delegation_id,
            "origin_channel": task.origin_channel,
            "origin_chat_id": task.origin_chat_id,
            "delegated_channel": task.delegated_channel,
            "delegated_task_id": task.delegated_task_id,
            "delegated_agent_id": task.delegated_agent_id,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "completed_at": task.completed_at,
            "metadata": task.metadata,
        }
        if extra:
            row["extra"] = dict(extra)

        try:
            path = self._audit_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("delegation.audit append failed event={} task_id={} err={}", event, task.id, exc)

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:12]

    @staticmethod
    def _extract_delegation_id_candidates(metadata: Mapping[str, Any] | None) -> list[str]:
        if not isinstance(metadata, Mapping):
            return []

        out: list[str] = []

        def add(value: Any) -> None:
            if isinstance(value, str):
                v = value.strip()
                if v and v not in out:
                    out.append(v)

        add(metadata.get("delegation_id"))

        raw_a2a = metadata.get("_a2a")
        if isinstance(raw_a2a, Mapping):
            add(raw_a2a.get("delegation_id"))

        raw_delegation = metadata.get("_delegation")
        if isinstance(raw_delegation, Mapping):
            add(raw_delegation.get("delegation_id"))

        return out


# Backward compatibility alias (historical name used across codebase).
DelegationTaskQueue = DelegationTaskMap


