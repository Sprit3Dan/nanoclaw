from __future__ import annotations

import pytest

from nanobot.channels.delegation_status_machine import DelegationStatusMachine


class _Clock:
    def __init__(self, start: float = 1_700_000_000.0, step: float = 1.0) -> None:
        self._value = start
        self._step = step

    def __call__(self) -> float:
        current = self._value
        self._value += self._step
        return current


def test_status_machine_happy_path_origin_and_receiver_flow() -> None:
    origin_clock = _Clock()
    receiver_clock = _Clock(start=1_700_000_100.0)

    origin = DelegationStatusMachine(now_fn=origin_clock)
    receiver = DelegationStatusMachine(now_fn=receiver_clock)

    delegation_id = "dlg-123"

    dispatched = origin.mark_dispatched(
        delegation_id,
        origin_agent="orchestrator",
        target_agent="aircraft",
        payload={"phase": "dispatch"},
    )
    assert dispatched.changed is True
    assert dispatched.previous_status is None
    assert dispatched.current_status == "dispatched"

    received_transition, received_event = receiver.mark_received(
        delegation_id,
        local_agent="aircraft",
        origin_agent="orchestrator",
        payload={"phase": "received"},
    )
    assert received_transition.changed is True
    assert received_transition.current_status == "received"
    assert received_event.status == "received"
    assert received_event.from_agent == "aircraft"
    assert received_event.to_agent == "orchestrator"

    origin_received = origin.apply_status_event(received_event.to_dict())
    assert origin_received is not None
    assert origin_received.changed is True
    assert origin_received.previous_status == "dispatched"
    assert origin_received.current_status == "received"

    done_transition, done_event = receiver.mark_done(
        delegation_id,
        local_agent="aircraft",
        origin_agent="orchestrator",
        payload={"phase": "done"},
    )
    assert done_transition.changed is True
    assert done_transition.previous_status == "received"
    assert done_transition.current_status == "done"
    assert done_event.status == "done"

    origin_done = origin.apply_status_event(done_event.to_dict())
    assert origin_done is not None
    assert origin_done.changed is True
    assert origin_done.previous_status == "received"
    assert origin_done.current_status == "done"

    origin_record = origin.get(delegation_id)
    receiver_record = receiver.get(delegation_id)

    assert origin_record is not None
    assert receiver_record is not None
    assert origin_record.status == "done"
    assert receiver_record.status == "done"


def test_status_machine_idempotent_and_regressive_updates_are_noop() -> None:
    machine = DelegationStatusMachine(now_fn=_Clock())
    delegation_id = "dlg-guards"

    first = machine.mark_dispatched(delegation_id, origin_agent="orchestrator")
    assert first.changed is True
    assert first.current_status == "dispatched"

    same = machine.mark_dispatched(delegation_id, origin_agent="orchestrator")
    assert same.changed is False
    assert same.current_status == "dispatched"

    done_transition, _ = machine.mark_done(
        delegation_id,
        local_agent="worker",
        origin_agent="orchestrator",
    )
    assert done_transition.changed is True
    assert done_transition.current_status == "done"

    regressive, _ = machine.mark_received(
        delegation_id,
        local_agent="worker",
        origin_agent="orchestrator",
    )
    assert regressive.changed is False
    assert regressive.current_status == "done"


def test_apply_status_event_rejects_invalid_payloads() -> None:
    machine = DelegationStatusMachine(now_fn=_Clock())

    assert machine.apply_status_event({}) is None
    assert machine.apply_status_event({"delegation_id": "x", "status": ""}) is None
    assert machine.apply_status_event({"delegation_id": "x", "status": "failed"}) is None

    ok = machine.apply_status_event(
        {
            "delegation_id": "x",
            "status": "dispatched",
            "from_agent": "orchestrator",
            "payload": {"phase": "dispatch"},
        }
    )
    assert ok is not None
    assert ok.changed is True
    assert ok.current_status == "dispatched"


def test_mark_methods_require_delegation_id() -> None:
    machine = DelegationStatusMachine(now_fn=_Clock())

    with pytest.raises(ValueError):
        machine.mark_dispatched("")

    with pytest.raises(ValueError):
        machine.mark_dispatched("   ")
