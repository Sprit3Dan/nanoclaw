"""Delegation status state machine for A2A lifecycle coordination.

This module centralizes delegation status transitions so both origin and receiver
agents execute the same rules.

Allowed lifecycle statuses (and only these):
    dispatched -> received -> done

Design goals:
- Monotonic progression (never regress to an earlier status).
- Idempotent updates (same status can be applied repeatedly with no side effects).
- Single helper to create outbound status events and apply inbound status events.
- Usable on both sides (origin and receiver) with identical behavior.

Typical flow:
1) Origin sends delegation payload:
      machine.mark_dispatched(...)
2) Receiver accepts payload:
      machine.mark_received(..., origin_agent=...)
      -> publish emitted "received" event to origin
3) Receiver completes work:
      machine.mark_done(..., origin_agent=...)
      -> publish emitted "done" event to origin
4) Origin consumes inbound status events:
      machine.apply_status_event(event)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from time import time
from typing import Any, Callable, Literal

DelegationStatus = Literal["dispatched", "received", "done"]

_ALLOWED: set[str] = {"dispatched", "received", "done"}
_ORDER: dict[str, int] = {"dispatched": 1, "received": 2, "done": 3}


@dataclass(slots=True)
class StatusHistoryEntry:
    status: DelegationStatus
    at_ts: float
    source: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DelegationStatusRecord:
    delegation_id: str
    status: DelegationStatus
    created_at: float
    updated_at: float
    origin_agent: str = ""
    target_agent: str = ""
    history: list[StatusHistoryEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "delegation_id": self.delegation_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "origin_agent": self.origin_agent,
            "target_agent": self.target_agent,
            "history": [
                {
                    "status": h.status,
                    "at_ts": h.at_ts,
                    "source": h.source,
                    "payload": dict(h.payload),
                }
                for h in self.history
            ],
        }


@dataclass(slots=True)
class StatusTransition:
    delegation_id: str
    changed: bool
    previous_status: DelegationStatus | None
    current_status: DelegationStatus
    at_ts: float


@dataclass(slots=True)
class StatusEvent:
    delegation_id: str
    status: DelegationStatus
    from_agent: str
    to_agent: str
    timestamp: int
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "delegation_id": self.delegation_id,
            "status": self.status,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }


class DelegationStatusMachine:
    """Thread-safe, monotonic delegation status tracker."""

    def __init__(self, *, now_fn: Callable[[], float] = time) -> None:
        self._now = now_fn
        self._lock = RLock()
        self._records: dict[str, DelegationStatusRecord] = {}

    # -------------------------------------------------------------------------
    # Public lifecycle entry points
    # -------------------------------------------------------------------------

    def mark_dispatched(
        self,
        delegation_id: str,
        *,
        origin_agent: str = "",
        target_agent: str = "",
        source: str = "local.dispatched",
        payload: dict[str, Any] | None = None,
    ) -> StatusTransition:
        """Set/advance local state to 'dispatched' on origin side."""
        return self._advance(
            delegation_id=delegation_id,
            status="dispatched",
            source=source,
            origin_agent=origin_agent,
            target_agent=target_agent,
            payload=payload,
        )

    def mark_received(
        self,
        delegation_id: str,
        *,
        local_agent: str,
        origin_agent: str,
        source: str = "local.received",
        payload: dict[str, Any] | None = None,
    ) -> tuple[StatusTransition, StatusEvent]:
        """Set local state to 'received' and build event to publish upstream."""
        transition = self._advance(
            delegation_id=delegation_id,
            status="received",
            source=source,
            origin_agent=origin_agent,
            payload=payload,
        )
        event = self._build_event(
            delegation_id=delegation_id,
            status="received",
            from_agent=local_agent,
            to_agent=origin_agent,
            payload=payload,
        )
        return transition, event

    def mark_done(
        self,
        delegation_id: str,
        *,
        local_agent: str,
        origin_agent: str,
        source: str = "local.done",
        payload: dict[str, Any] | None = None,
    ) -> tuple[StatusTransition, StatusEvent]:
        """Set local state to 'done' and build terminal event to publish upstream."""
        transition = self._advance(
            delegation_id=delegation_id,
            status="done",
            source=source,
            origin_agent=origin_agent,
            payload=payload,
        )
        event = self._build_event(
            delegation_id=delegation_id,
            status="done",
            from_agent=local_agent,
            to_agent=origin_agent,
            payload=payload,
        )
        return transition, event

    def apply_status_event(
        self,
        event: dict[str, Any],
        *,
        source: str = "remote.status_event",
    ) -> StatusTransition | None:
        """Apply inbound status event from transport to local record."""
        did = str(event.get("delegation_id", "")).strip()
        status = str(event.get("status", "")).strip().lower()
        if not did or status not in _ALLOWED:
            return None

        from_agent = str(event.get("from_agent", "")).strip()
        payload = event.get("payload")
        payload_obj = payload if isinstance(payload, dict) else {}

        return self._advance(
            delegation_id=did,
            status=status,  # type: ignore[arg-type]
            source=source,
            target_agent=from_agent,
            payload=payload_obj,
        )

    # -------------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------------

    def get(self, delegation_id: str) -> DelegationStatusRecord | None:
        with self._lock:
            rec = self._records.get(str(delegation_id).strip())
            if rec is None:
                return None
            return DelegationStatusRecord(
                delegation_id=rec.delegation_id,
                status=rec.status,
                created_at=rec.created_at,
                updated_at=rec.updated_at,
                origin_agent=rec.origin_agent,
                target_agent=rec.target_agent,
                history=list(rec.history),
            )

    def list_records(self) -> list[DelegationStatusRecord]:
        with self._lock:
            return [self.get(k) for k in self._records.keys() if self.get(k) is not None]  # type: ignore[list-item]

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _build_event(
        self,
        *,
        delegation_id: str,
        status: DelegationStatus,
        from_agent: str,
        to_agent: str,
        payload: dict[str, Any] | None,
    ) -> StatusEvent:
        return StatusEvent(
            delegation_id=delegation_id,
            status=status,
            from_agent=from_agent,
            to_agent=to_agent,
            timestamp=int(self._now()),
            payload=dict(payload or {}),
        )

    def _advance(
        self,
        *,
        delegation_id: str,
        status: DelegationStatus,
        source: str,
        origin_agent: str = "",
        target_agent: str = "",
        payload: dict[str, Any] | None = None,
    ) -> StatusTransition:
        did = str(delegation_id).strip()
        if not did:
            raise ValueError("delegation_id is required")
        if status not in _ALLOWED:
            raise ValueError(f"invalid status: {status}")

        now = float(self._now())

        with self._lock:
            rec = self._records.get(did)
            if rec is None:
                rec = DelegationStatusRecord(
                    delegation_id=did,
                    status=status,
                    created_at=now,
                    updated_at=now,
                    origin_agent=origin_agent,
                    target_agent=target_agent,
                )
                rec.history.append(
                    StatusHistoryEntry(
                        status=status,
                        at_ts=now,
                        source=source,
                        payload=dict(payload or {}),
                    )
                )
                self._records[did] = rec
                return StatusTransition(
                    delegation_id=did,
                    changed=True,
                    previous_status=None,
                    current_status=status,
                    at_ts=now,
                )

            # Merge known agent identities when provided.
            if origin_agent and not rec.origin_agent:
                rec.origin_agent = origin_agent
            if target_agent and not rec.target_agent:
                rec.target_agent = target_agent

            prev = rec.status
            if _ORDER[status] > _ORDER[prev]:
                rec.status = status
                rec.updated_at = now
                rec.history.append(
                    StatusHistoryEntry(
                        status=status,
                        at_ts=now,
                        source=source,
                        payload=dict(payload or {}),
                    )
                )
                return StatusTransition(
                    delegation_id=did,
                    changed=True,
                    previous_status=prev,
                    current_status=status,
                    at_ts=now,
                )

            # Idempotent/no-op for same or regressive statuses.
            return StatusTransition(
                delegation_id=did,
                changed=False,
                previous_status=prev,
                current_status=prev,
                at_ts=now,
            )

