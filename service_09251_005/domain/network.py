"""道路网络：节点、有向可达性、封闭事件。

边具有方向性（双向/单向），封闭后两个方向均不可通行。
路径搜索采用 Dijkstra，距离即成本，用于估算救援队到场里程与拖车里程。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RoadNode:
    node_id: str
    name: str = ""


@dataclass
class RoadSegment:
    segment_id: str
    a: str
    b: str
    distance_km: float
    # bidirectional / atob（仅 a->b）/ btoa（仅 b->a）
    direction: str = "bidirectional"
    speed_kmh: float = 80.0
    closed: bool = False
    closed_since: Optional[str] = None  # ISO 时间戳，仅作记录
    closed_reason: str = ""

    def usable_a_to_b(self) -> bool:
        return not self.closed and self.direction in ("bidirectional", "atob")

    def usable_b_to_a(self) -> bool:
        return not self.closed and self.direction in ("bidirectional", "btoa")


@dataclass
class ClosureEvent:
    segment_id: str
    closed_at: str
    reason: str = ""
    reopened: bool = False


@dataclass
class Route:
    nodes: list[str]
    distance_km: float

    @property
    def reachable(self) -> bool:
        return bool(self.nodes)


class RoadNetwork:
    def __init__(self) -> None:
        self.nodes: dict[str, RoadNode] = {}
        self.segments: dict[str, RoadSegment] = {}
        self.closures: list[ClosureEvent] = []

    # ---- 变更 ----
    def add_node(self, node_id: str, name: str = "") -> RoadNode:
        node = RoadNode(node_id=node_id, name=name)
        self.nodes[node_id] = node
        return node

    def add_segment(
        self,
        segment_id: str,
        a: str,
        b: str,
        distance_km: float,
        direction: str = "bidirectional",
        speed_kmh: float = 80.0,
    ) -> RoadSegment:
        if a not in self.nodes:
            self.add_node(a)
        if b not in self.nodes:
            self.add_node(b)
        seg = RoadSegment(segment_id, a, b, distance_km, direction, speed_kmh)
        self.segments[segment_id] = seg
        return seg

    def close_segment(self, segment_id: str, closed_at: str, reason: str = "") -> None:
        seg = self.segments[segment_id]
        if not seg.closed:
            seg.closed = True
            seg.closed_since = closed_at
            seg.closed_reason = reason
            self.closures.append(ClosureEvent(segment_id, closed_at, reason))

    def reopen_segment(self, segment_id: str) -> None:
        seg = self.segments[segment_id]
        seg.closed = False
        seg.closed_since = None
        seg.closed_reason = ""
        for ev in reversed(self.closures):
            if ev.segment_id == segment_id and not ev.reopened:
                ev.reopened = True
                break

    # ---- 查询 ----
    def _neighbors(self, node_id: str) -> list[tuple[str, float, str]]:
        out: list[tuple[str, float, str]] = []
        for seg in self.segments.values():
            if seg.a == node_id and seg.usable_a_to_b():
                out.append((seg.b, seg.distance_km, seg.segment_id))
            elif seg.b == node_id and seg.usable_b_to_a():
                out.append((seg.a, seg.distance_km, seg.segment_id))
        return out

    def shortest_route(self, origin: str, target: str) -> Route:
        """返回当前网络下的最短可达路径；不可达时 nodes 为空。"""
        if origin not in self.nodes or target not in self.nodes:
            return Route([], float("inf"))
        if origin == target:
            return Route([origin], 0.0)
        dist: dict[str, float] = {origin: 0.0}
        prev: dict[str, str] = {}
        pq: list[tuple[float, str]] = [(0.0, origin)]
        while pq:
            d, cur = heapq.heappop(pq)
            if d > dist.get(cur, float("inf")):
                continue
            if cur == target:
                break
            for nxt, edge_km, _seg_id in self._neighbors(cur):
                nd = d + edge_km
                if nd < dist.get(nxt, float("inf")):
                    dist[nxt] = nd
                    prev[nxt] = cur
                    heapq.heappush(pq, (nd, nxt))
        if target not in dist:
            return Route([], float("inf"))
        path = [target]
        while path[-1] != origin:
            path.append(prev[path[-1]])
        path.reverse()
        return Route(path, dist[target])

    def is_reachable(self, origin: str, target: str) -> bool:
        return self.shortest_route(origin, target).reachable

    def route_segments(self, route: Route) -> list[str]:
        """路径经过的边 id 列表。"""
        result: list[str] = []
        for u, v in zip(route.nodes, route.nodes[1:]):
            for seg in self.segments.values():
                if {u, v} == {seg.a, seg.b}:
                    result.append(seg.segment_id)
                    break
        return result
