"""FastAPI service for reconciling cross-system event timelines."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import timeutils as tu
from .schemas import (
    AuditRequest,
    ConflictLoop,
    ConflictStep,
    ErrorDetail,
    ErrorEnvelope,
    FeasibleAudit,
    GapConstraint,
    InfeasibleAudit,
    TightenedEvent,
)
from .solver import Edge, Feasible, Infeasible, solve

SERVICE_NAME = "event-timeline-audit"
ANCHOR_ID = "<epoch>"


def _error(status_code: int, code: str, message: str, details: dict[str, Any] | None = None):
    return JSONResponse(
        status_code=status_code,
        content=ErrorEnvelope(error=ErrorDetail(code=code, message=message, details=details)).model_dump(),
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="Event Timeline Audit",
        version="1.0.0",
        description=(
            "Check whether cross-system event times and gap constraints can all hold. "
            "Constraints are reduced to a difference-constraint system and solved with "
            "Bellman-Ford. Infeasible requests return the original constraints forming "
            "the contradictory closed loop."
        ),
    )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return _error(
            422,
            "invalid_request",
            "request body failed validation",
            {"errors": jsonable_encoder(exc.errors())},
        )

    @app.get("/healthz", tags=["health"])
    @app.get("/", tags=["health"])
    async def healthz():
        return {"status": "ok", "service": SERVICE_NAME}

    @app.post(
        "/audits",
        response_model=FeasibleAudit | InfeasibleAudit,
        tags=["audits"],
    )
    async def create_audit(payload: AuditRequest):
        # ---- semantic validation: duplicate IDs and unknown references ----
        seen_event_ids: set[str] = set()
        duplicate_event_ids: list[str] = []
        for event in payload.events:
            if event.id in seen_event_ids and event.id not in duplicate_event_ids:
                duplicate_event_ids.append(event.id)
            seen_event_ids.add(event.id)
        if duplicate_event_ids:
            return _error(
                422,
                "duplicate_event_id",
                "every event id must be unique",
                {"duplicate_event_ids": duplicate_event_ids},
            )

        seen_constraint_ids: set[str] = set()
        duplicate_constraint_ids: list[str] = []
        unknown_refs: list[dict[str, str]] = []
        for index, constraint in enumerate(payload.constraints):
            if constraint.id is not None:
                if constraint.id in seen_constraint_ids and constraint.id not in duplicate_constraint_ids:
                    duplicate_constraint_ids.append(constraint.id)
                seen_constraint_ids.add(constraint.id)
            for side, event_id in (
                ("left_event", constraint.left_event),
                ("right_event", constraint.right_event),
            ):
                if event_id not in seen_event_ids:
                    unknown_refs.append(
                        {"constraint_index": str(index), side: event_id}
                    )
        if unknown_refs:
            return _error(
                422,
                "unknown_event_reference",
                "a constraint references an event id that is not defined",
                {"references": unknown_refs},
            )
        if duplicate_constraint_ids:
            return _error(
                422,
                "duplicate_constraint_id",
                "every constraint id must be unique when present",
                {"duplicate_constraint_ids": duplicate_constraint_ids},
            )

        # ---- build nodes: node 0 is the epoch anchor x[0] = 0 ----
        node_of = {event.id: i + 1 for i, event in enumerate(payload.events)}
        n_nodes = len(payload.events) + 1
        edges: list[Edge] = []

        for event in payload.events:
            v = node_of[event.id]
            earliest = tu.to_micros(event.earliest)
            latest = tu.to_micros(event.latest)
            # x[v] <= latest  <=>  x[v] - x[0] <= latest : 0 -> v, latest
            edges.append(
                Edge(
                    u=0,
                    v=v,
                    w=latest,
                    origin={
                        "kind": "latest",
                        "event": event.id,
                        "bound": tu.format_micros(latest),
                    },
                )
            )
            # x[v] >= earliest  <=>  x[0] - x[v] <= -earliest : v -> 0, -earliest
            edges.append(
                Edge(
                    u=v,
                    v=0,
                    w=-earliest,
                    origin={
                        "kind": "earliest",
                        "event": event.id,
                        "bound": tu.format_micros(earliest),
                    },
                )
            )

        for index, constraint in enumerate(payload.constraints):
            a = node_of[constraint.left_event]
            b = node_of[constraint.right_event]
            gap = tu.seconds_to_micros(constraint.seconds)
            origin = _constraint_origin(index, constraint, gap)
            if constraint.type == "at_most":
                # x[b] - x[a] <= gap : a -> b, gap
                edges.append(Edge(u=a, v=b, w=gap, origin=origin))
            else:
                # x[b] - x[a] >= gap  <=>  x[a] - x[b] <= -gap : b -> a, -gap
                edges.append(Edge(u=b, v=a, w=-gap, origin=origin))

        result = solve(n_nodes, edges)
        if isinstance(result, Feasible):
            return _feasible_response(payload, result, node_of)
        return _infeasible_response(payload, edges, result, node_of)

    return app


def _constraint_origin(index: int, constraint: GapConstraint, gap_micros: int) -> dict[str, Any]:
    return {
        "kind": constraint.type,
        "constraint_id": constraint.id,
        "constraint_index": index,
        "left_event": constraint.left_event,
        "right_event": constraint.right_event,
        "seconds": tu.format_seconds(gap_micros),
    }


def _feasible_response(
    payload: AuditRequest, result: Feasible, node_of: dict[str, int]
) -> FeasibleAudit:
    tightened: list[TightenedEvent] = []
    for event in payload.events:
        v = node_of[event.id]
        tightened.append(
            TightenedEvent(
                id=event.id,
                earliest=tu.format_micros(result.lower[v]),
                latest=tu.format_micros(result.upper[v]),
                feasible_time=tu.format_micros(result.assignment[v]),
            )
        )
    return FeasibleAudit(events=tightened)


def _inequality_for(origin: dict[str, Any]) -> str:
    kind = origin["kind"]
    if kind == "earliest":
        return f"{origin['event']} >= {origin['bound']}"
    if kind == "latest":
        return f"{origin['event']} <= {origin['bound']}"
    if kind == "at_least":
        return f"{origin['right_event']} - {origin['left_event']} >= {origin['seconds']}s"
    return f"{origin['right_event']} - {origin['left_event']} <= {origin['seconds']}s"


def _infeasible_response(
    payload: AuditRequest,
    edges: list[Edge],
    result: Infeasible,
    node_of: dict[str, int],
) -> InfeasibleAudit:
    id_of_node = {node: event_id for event_id, node in node_of.items()}
    id_of_node[0] = ANCHOR_ID

    # The solver returns the loop in edge direction: edge k runs
    # chain[k] -> chain[k+1], and the last edge closes back onto chain[0].
    ordered_edges = result.cycle_edges
    if not ordered_edges:  # pragma: no cover - defensive
        raise RuntimeError("extracted contradiction loop is empty")

    chain_events = [id_of_node[node] for node in result.cycle_nodes]
    chain_events.append(chain_events[0])
    for k, edge_idx in enumerate(ordered_edges):
        edge = edges[edge_idx]
        assert id_of_node[edge.u] == chain_events[k]
        assert id_of_node[edge.v] == chain_events[k + 1]

    steps: list[ConflictStep] = []
    for edge_idx in ordered_edges:
        origin = edges[edge_idx].origin
        steps.append(
            ConflictStep(
                kind=origin["kind"],
                constraint_index=origin.get("constraint_index"),
                constraint_id=origin.get("constraint_id"),
                event=origin.get("event"),
                left_event=origin.get("left_event"),
                right_event=origin.get("right_event"),
                bound=origin.get("bound"),
                seconds=origin.get("seconds"),
                inequality=_inequality_for(origin),
            )
        )

    total_micros = sum(edges[i].w for i in ordered_edges)
    total_seconds = tu.format_seconds(total_micros)
    loop = "  ".join(step.inequality for step in steps)
    contradiction = (
        "Summing the inequalities around the closed loop gives 0 <= "
        f"{total_seconds}s; all timestamps cancel, so the constraints cannot hold together. "
        f"Loop: {loop}"
    )

    return InfeasibleAudit(
        conflict=ConflictLoop(
            events=chain_events,
            steps=steps,
            total_seconds=total_seconds,
            contradiction=contradiction,
        )
    )


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "info"),
    )
