"""An empty successful model reply is a bounded parse failure, never a no-op."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from memgarden import CaptureRequest, GardenComponent, MaintenanceRequest


class Replies:
    def __init__(self, *values):
        self.values = values
        self.calls = 0

    def complete(self, prompt, *, purpose):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


class AsyncReplies(Replies):
    async def complete(self, prompt, *, purpose):
        return super().complete(prompt, purpose=purpose)


def request(lane):
    if lane == "capture":
        return CaptureRequest(window="I avoid spicy food.", locale="en")
    return MaintenanceRequest(cards=[
        {"id": f"m_{i}", "summary": "Food preference", "content": "Avoid spicy food.",
         "created_at": "2026-08-01"} for i in range(15)
    ], locale="en")


def valid_reply(lane):
    if lane == "capture":
        return json.dumps({"cards": [{
            "action": "add", "bucket": "Preferences", "summary": "Avoids spicy food",
            "content": "Spicy food causes stomach pain, so choose mild dishes.",
        }]})
    return json.dumps({"consolidations": []})


def session_for(garden, lane):
    method = garden.capture_session if lane == "capture" else garden.maintenance_session
    return method(request(lane))


@pytest.mark.parametrize("lane,mode", [
    ("capture", "sync"), ("capture", "async"), ("capture", "host"),
    ("maintenance", "sync"), ("maintenance", "host"),
])
def test_empty_success_gets_one_corrective_reply(lane, mode):
    model = (AsyncReplies if mode == "async" else Replies)(" \n\t", valid_reply(lane))
    garden = GardenComponent(model=model)
    if mode == "host":
        session = session_for(garden, lane)
        while (prompt := session.next_prompt()) is not None:
            assert model.calls < 2
            session.feed(model.complete(prompt, purpose=lane))
        result = session.result()
    elif mode == "async":
        result = asyncio.run(garden.acapture(request(lane)))
    else:
        method = garden.capture if lane == "capture" else garden.run_maintenance
        result = method(request(lane))
    assert model.calls == 2
    assert result.error is None
    if lane == "capture":
        assert len(result.mutations) == 1 and not result.nothing_worth_keeping
    else:
        assert result.needed and not result.mutations  # explicit JSON no-op, not empty text


@pytest.mark.parametrize("lane", ["capture", "maintenance"])
@pytest.mark.parametrize("budget", [0, 1])
def test_repeated_empty_replies_fail_within_existing_budget(lane, budget):
    garden = GardenComponent(model=Replies(""), max_capture_retries=budget)
    session = session_for(garden, lane)
    calls = 0
    while session.next_prompt() is not None:
        calls += 1
        assert calls <= budget + 1
        session.feed("")
    result = session.result()
    assert calls == budget + 1
    assert result.error and "no_json_object" in result.error
    assert not result.mutations
    if lane == "capture":
        assert not result.nothing_worth_keeping


@pytest.mark.parametrize("lane", ["capture", "maintenance"])
def test_nonempty_prose_keeps_existing_failure_policy(lane):
    session = session_for(GardenComponent(model=Replies("")), lane)
    assert session.next_prompt() is not None
    session.feed("This is not JSON.")
    assert session.next_prompt() is None
    assert session.result().error == "no_json_object"


@pytest.mark.parametrize("lane", ["capture", "maintenance"])
def test_truncation_and_empty_reply_share_one_retry_budget(lane):
    session = session_for(GardenComponent(model=Replies("")), lane)
    assert session.next_prompt() is not None
    session.feed("", truncated=True)
    assert session.next_prompt() is not None
    session.feed("")
    assert session.next_prompt() is None
    assert session.result().error and not session.result().mutations


@pytest.mark.parametrize("lane", ["capture", "maintenance"])
@pytest.mark.parametrize("envelope", [dict, SimpleNamespace])
@pytest.mark.parametrize("truncated", [False, True])
def test_sdk_unwraps_reply_envelopes_after_empty_or_truncated_reply(lane, envelope, truncated):
    model = Replies(envelope(text="", truncated=truncated),
                    envelope(text=valid_reply(lane), truncated=False))
    garden = GardenComponent(model=model)
    method = garden.capture if lane == "capture" else garden.run_maintenance
    result = method(request(lane))
    assert model.calls == 2 and result.error is None
    if lane == "capture":
        assert len(result.mutations) == 1
    else:
        assert result.needed
