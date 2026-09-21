"""Difference-constraint solver for event timelines.

Every constraint is reduced to an inequality of the form

    x[v] - x[u] <= w

where x[i] is the timestamp of event ``i`` in integer microseconds and
node 0 is a fixed epoch anchor (x[0] = 0). Each inequality becomes a
directed, weighted edge ``u -> v`` with weight ``w``:

* ``earliest = E``              ->  v -> 0  with weight -E   (x[v] >= E)
* ``latest   = L``              ->  0 -> v  with weight  L   (x[v] <= L)
* at_least gap ``d``  a -> b    ->  b -> a  with weight -d   (x[b] - x[a] >= d)
* at_most  gap ``d``  a -> b    ->  a -> b  with weight  d   (x[b] - x[a] <= d)

A system is feasible iff the constraint graph contains no reachable
negative-weight cycle. Every event carries both an earliest and a latest
bound, so node 0 reaches every node directly and a Bellman-Ford run from
node 0 decides feasibility.

On a feasible system the tightened range of event v is

    lower[v] = -dist_rev[v] <= x[v] <= dist_fwd[v] = upper[v]

where dist_fwd is the shortest-path distance from node 0 in the graph and
dist_rev the shortest-path distance from node 0 in the reversed graph.
The forward distances themselves form a concrete feasible assignment.

On an infeasible system the predecessor chain of an edge that still
relaxes on the V-th pass yields a closed loop of original constraints
whose inequalities sum to ``0 <= (negative number)`` — the evidence
conflict to report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Edge:
    u: int
    v: int
    w: int
    origin: dict[str, Any] = field(default_factory=dict)


@dataclass
class Feasible:
    feasible: bool = True
    upper: list[int] = field(default_factory=list)
    lower: list[int] = field(default_factory=list)
    assignment: list[int] = field(default_factory=list)


@dataclass
class Infeasible:
    feasible: bool = False
    # cycle_edges[k] runs cycle_nodes[k] -> cycle_nodes[k+1] in edge
    # direction (k+1 wraps), so the inequalities telescope into
    # 0 <= (negative sum).
    cycle_edges: list[int] = field(default_factory=list)
    cycle_nodes: list[int] = field(default_factory=list)


def _relax(
    edges: list[Edge],
    dist: list[Optional[int]],
    pred_edge: list[Optional[int]],
) -> bool:
    changed = False
    for idx, edge in enumerate(edges):
        du = dist[edge.u]
        if du is None:
            continue
        candidate = du + edge.w
        dv = dist[edge.v]
        if dv is None or candidate < dv:
            dist[edge.v] = candidate
            pred_edge[edge.v] = idx
            changed = True
    return changed


def _extract_cycle(
    edges: list[Edge],
    pred_edge: list[Optional[int]],
    start: int,
    n_nodes: int,
) -> tuple[list[int], list[int]]:
    """Recover a negative-weight cycle from predecessor pointers.

    Returns ``(cycle_edges, cycle_nodes)`` in edge direction: for every k,
    edge ``cycle_edges[k]`` runs ``cycle_nodes[k] -> cycle_nodes[k+1]``
    (indices wrap around). The predecessor pointers are collected walking
    backwards, so the gathered lists are reversed before returning.
    """
    # Walking n predecessor steps from a node updated on the n-th pass is
    # guaranteed to land on a node that belongs to the offending cycle.
    cur = start
    for _ in range(n_nodes):
        edge_idx = pred_edge[cur]
        if edge_idx is None:  # pragma: no cover - defensive
            break
        cur = edges[edge_idx].u

    backwards_edges: list[int] = []
    entry = cur
    while True:
        edge_idx = pred_edge[cur]
        if edge_idx is None:  # pragma: no cover - defensive
            break
        backwards_edges.append(edge_idx)
        cur = edges[edge_idx].u
        if cur == entry or len(backwards_edges) > n_nodes:
            break

    cycle_edges = list(reversed(backwards_edges))
    cycle_nodes = [edges[cycle_edges[0]].u]
    for idx in cycle_edges:
        edge = edges[idx]
        assert edge.u == cycle_nodes[-1], "cycle step must continue the walk"
        cycle_nodes.append(edge.v)
    assert cycle_nodes[-1] == cycle_nodes[0], "extracted walk must close"
    return cycle_edges, cycle_nodes[:-1]


def _detect_negative_cycle(n_nodes: int, edges: list[Edge]) -> Optional[tuple[list[int], list[int]]]:
    """Return an edge/node loop describing a negative cycle, if any.

    Uses the implicit-super-source initialization (all distances 0): a
    negative cycle relaxes an edge within V passes regardless of
    reachability, which makes this a robust detector.
    """
    dist: list[Optional[int]] = [0] * n_nodes
    pred_edge: list[Optional[int]] = [None] * n_nodes

    for _ in range(n_nodes):
        if not _relax(edges, dist, pred_edge):
            return None  # converged within V-1 passes => no negative cycle

    # An edge relaxed on the V-th pass leads into a negative cycle.
    start = next(
        edge.v
        for edge in edges
        if dist[edge.u] is not None and dist[edge.v] is not None
        and dist[edge.u] + edge.w < dist[edge.v]
    )
    cycle_edges, cycle_nodes = _extract_cycle(edges, pred_edge, start, n_nodes)

    if sum(edges[i].w for i in cycle_edges) < 0 and cycle_edges:
        return cycle_edges, cycle_nodes

    # Defensive: try every still-relaxable edge as the chain entry point.
    for edge in edges:
        du, dv = dist[edge.u], dist[edge.v]
        if du is not None and dv is not None and du + edge.w < dv:
            candidate_edges, candidate_nodes = _extract_cycle(
                edges, pred_edge, edge.v, n_nodes
            )
            if candidate_edges and sum(edges[i].w for i in candidate_edges) < 0:
                return candidate_edges, candidate_nodes
    return None  # pragma: no cover - defensive


def _shortest_from_zero(n_nodes: int, edges: list[Edge], reverse: bool) -> Optional[list[int]]:
    """Bellman-Ford from anchor node 0; returns None on a negative cycle."""
    run_edges = (
        [Edge(u=e.v, v=e.u, w=e.w, origin=e.origin) for e in edges]
        if reverse
        else edges
    )
    dist: list[Optional[int]] = [0] + [None] * (n_nodes - 1)
    for _ in range(n_nodes - 1):
        if not _relax(run_edges, dist, [None] * n_nodes):
            break

    for edge in run_edges:
        du = dist[edge.u]
        dv = dist[edge.v]
        if du is not None and (dv is None or du + edge.w < dv):
            return None

    assert all(d is not None for d in dist), "anchor node must reach every event"
    return [d for d in dist if d is not None]


def solve(n_nodes: int, edges: list[Edge]) -> Feasible | Infeasible:
    forward = _shortest_from_zero(n_nodes, edges, reverse=False)
    reverse = None
    if forward is not None:
        reverse = _shortest_from_zero(n_nodes, edges, reverse=True)

    if forward is not None and reverse is not None:
        return Feasible(
            upper=forward,
            lower=[-d for d in reverse],
            assignment=list(forward),
        )

    detected = _detect_negative_cycle(n_nodes, edges)
    if detected is None:  # pragma: no cover - defensive, should be impossible
        raise RuntimeError("infeasibility detected but no negative cycle could be extracted")
    cycle_edges, cycle_nodes = detected
    return Infeasible(cycle_edges=cycle_edges, cycle_nodes=cycle_nodes)
