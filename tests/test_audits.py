from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _event(event_id, earliest="2026-09-21T10:00:00Z", latest="2026-09-21T12:00:00Z"):
    return {"id": event_id, "earliest": earliest, "latest": latest}


def test_no_constraints_returns_original_bounds():
    response = client.post(
        "/audits",
        json={
            "events": [
                _event("a", "2026-09-21T10:00:00Z", "2026-09-21T12:00:00Z"),
            ],
            "constraints": [],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["feasible"] is True
    event = body["events"][0]
    assert event["earliest"] == "2026-09-21T10:00:00Z"
    assert event["latest"] == "2026-09-21T12:00:00Z"


def test_at_most_constraint_tightens_range_across_events():
    response = client.post(
        "/audits",
        json={
            "events": [
                _event("a", "2026-09-21T10:00:00Z", "2026-09-21T12:00:00Z"),
                _event("b", "2026-09-21T10:30:00Z", "2026-09-21T13:00:00Z"),
            ],
            "constraints": [
                {
                    "type": "at_most",
                    "left_event": "a",
                    "right_event": "b",
                    "seconds": "300",
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["feasible"] is True
    by_id = {event["id"]: event for event in body["events"]}
    # b - a <= 300:
    #   b.latest <= a.latest + 300 = 12:05 (tightened from 13:00)
    assert by_id["b"]["latest"] == "2026-09-21T12:05:00Z"
    #   a.earliest >= b.earliest - 300 = 10:25 (tightened from 10:00)
    assert by_id["a"]["earliest"] == "2026-09-21T10:25:00Z"
    # a.latest is not tightened by this direction
    assert by_id["a"]["latest"] == "2026-09-21T12:00:00Z"
    assert by_id["b"]["earliest"] == "2026-09-21T10:30:00Z"
    # the reported assignment must satisfy the gap
    assert by_id["b"]["feasible_time"] > by_id["a"]["feasible_time"]


def test_at_least_constraint_tightens_bounds():
    response = client.post(
        "/audits",
        json={
            "events": [
                _event("a", "2026-09-21T10:00:00Z", "2026-09-21T11:00:00Z"),
                _event("b", "2026-09-21T10:00:00Z", "2026-09-21T11:00:00Z"),
            ],
            "constraints": [
                {
                    "id": "gap-1",
                    "type": "at_least",
                    "left_event": "a",
                    "right_event": "b",
                    "seconds": "600",
                }
            ],
        },
    )
    assert response.status_code == 200
    by_id = {event["id"]: event for event in response.json()["events"]}
    # b >= a + 600: a.latest = b.latest - 600 = 10:50
    assert by_id["a"]["latest"] == "2026-09-21T10:50:00Z"
    # b.earliest = a.earliest + 600 = 10:10
    assert by_id["b"]["earliest"] == "2026-09-21T10:10:00Z"


def test_microsecond_interval_is_accepted():
    response = client.post(
        "/audits",
        json={
            "events": [
                _event("a", "2026-09-21T10:00:00.000000Z", "2026-09-21T10:00:01Z"),
                _event("b", "2026-09-21T10:00:00.000000Z", "2026-09-21T10:00:01Z"),
            ],
            "constraints": [
                {
                    "type": "at_most",
                    "left_event": "a",
                    "right_event": "b",
                    "seconds": "0.000001",
                }
            ],
        },
    )
    assert response.status_code == 200


def test_offset_other_than_utc_is_normalized():
    response = client.post(
        "/audits",
        json={
            "events": [
                _event("a", "2026-09-21T12:00:00+02:00", "2026-09-21T14:00:00+02:00"),
            ],
        },
    )
    assert response.status_code == 200
    event = response.json()["events"][0]
    assert event["earliest"] == "2026-09-21T10:00:00Z"
    assert event["latest"] == "2026-09-21T12:00:00Z"


def test_naive_timestamp_is_rejected():
    response = client.post(
        "/audits",
        json={"events": [_event("a", "2026-09-21T10:00:00", "2026-09-21T12:00:00Z")]},
    )
    assert response.status_code == 422


def test_inconsistent_bounds_become_conflict_loop():
    # earliest > latest is a negative two-edge cycle through the epoch anchor
    response = client.post(
        "/audits",
        json={
            "events": [
                _event("a", "2026-09-21T12:00:00Z", "2026-09-21T10:00:00Z"),
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["feasible"] is False
    conflict = body["conflict"]
    assert conflict["events"][0] == conflict["events"][-1]
    # earliest/latest contradict through the epoch anchor
    assert set(conflict["events"]) == {"a", "<epoch>"}
    assert Decimal_(conflict["total_seconds"]) < 0
    kinds = {step["kind"] for step in conflict["steps"]}
    assert kinds == {"earliest", "latest"}


def Decimal_(text):
    from decimal import Decimal

    return Decimal(text)


def _involved_event(step):
    if step["kind"] in ("earliest", "latest"):
        return step["event"]
    return (step["left_event"], step["right_event"])


def test_conflict_loop_is_a_closed_walk_with_negative_sum():
    response = client.post(
        "/audits",
        json={
            "events": [
                _event("a", "2026-09-21T10:00:00Z", "2026-09-21T12:00:00Z"),
                _event("b", "2026-09-21T10:00:00Z", "2026-09-21T12:00:00Z"),
                _event("c", "2026-09-21T10:00:00Z", "2026-09-21T12:00:00Z"),
            ],
            "constraints": [
                {"id": "c1", "type": "at_most", "left_event": "a", "right_event": "b", "seconds": "100"},
                {"id": "c2", "type": "at_most", "left_event": "b", "right_event": "c", "seconds": "100"},
                {"id": "c4", "type": "at_least", "left_event": "a", "right_event": "c", "seconds": "1000"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["feasible"] is False
    conflict = body["conflict"]
    steps = conflict["steps"]
    events = conflict["events"]
    assert len(steps) == len(events) - 1
    assert events[0] == events[-1]
    # the three original constraints are exactly the contradictory loop
    assert {step["constraint_id"] for step in steps} == {"c1", "c2", "c4"}

    def ends(step):
        """Return (from_event, to_event) along the edge direction."""
        if step["kind"] == "earliest":  # event -> anchor
            return step["event"], "<epoch>"
        if step["kind"] == "latest":  # anchor -> event
            return "<epoch>", step["event"]
        if step["kind"] == "at_most":  # left -> right
            return step["left_event"], step["right_event"]
        return step["right_event"], step["left_event"]  # at_least: right -> left

    # each step connects chain[i] -> chain[i+1], and the loop closes
    for i, step in enumerate(steps):
        src, dst = ends(step)
        assert (src, dst) == (events[i], events[i + 1])
    assert Decimal_(conflict["total_seconds"]) < 0


def test_unknown_event_reference_is_rejected():
    response = client.post(
        "/audits",
        json={
            "events": [_event("a")],
            "constraints": [
                {"type": "at_most", "left_event": "a", "right_event": "ghost", "seconds": "1"}
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_event_reference"


def test_duplicate_event_id_is_rejected():
    response = client.post(
        "/audits",
        json={"events": [_event("a"), _event("a")]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "duplicate_event_id"


def test_duplicate_constraint_id_is_rejected():
    response = client.post(
        "/audits",
        json={
            "events": [_event("a"), _event("b")],
            "constraints": [
                {"id": "same", "type": "at_most", "left_event": "a", "right_event": "b", "seconds": "1"},
                {"id": "same", "type": "at_least", "left_event": "a", "right_event": "b", "seconds": "1"},
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "duplicate_constraint_id"


def test_sub_microsecond_resolution_is_rejected():
    response = client.post(
        "/audits",
        json={
            "events": [_event("a"), _event("b")],
            "constraints": [
                {"type": "at_most", "left_event": "a", "right_event": "b", "seconds": "0.0000001"}
            ],
        },
    )
    assert response.status_code == 422


def test_negative_gap_is_rejected():
    response = client.post(
        "/audits",
        json={
            "events": [_event("a"), _event("b")],
            "constraints": [
                {"type": "at_most", "left_event": "a", "right_event": "b", "seconds": "-5"}
            ],
        },
    )
    assert response.status_code == 422


def test_self_constraint_with_positive_at_least_is_infeasible():
    # x - x >= d with d > 0 is the negative self-loop 0 <= -d
    response = client.post(
        "/audits",
        json={
            "events": [_event("a")],
            "constraints": [
                {"id": "self", "type": "at_least", "left_event": "a", "right_event": "a", "seconds": "5"}
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["feasible"] is False
    assert body["conflict"]["events"] == ["a", "a"]
    assert body["conflict"]["steps"][0]["constraint_id"] == "self"
    assert Decimal_(body["conflict"]["total_seconds"]) == -5


def test_zero_gap_self_constraint_is_feasible():
    response = client.post(
        "/audits",
        json={
            "events": [_event("a")],
            "constraints": [
                {"type": "at_most", "left_event": "a", "right_event": "a", "seconds": "0"}
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["feasible"] is True


def test_empty_event_list_is_rejected():
    response = client.post("/audits", json={"events": []})
    assert response.status_code == 422
