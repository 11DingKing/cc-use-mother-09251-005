"""SQLite 持久化：派单状态、责任交接链、升级进度均可跨重启恢复。

运行数据写入调用方指定的数据目录（默认系统临时目录），不污染源码目录。
并发安全由单连接 + 可重入锁保证；抢单等关键竞争通过条件 UPDATE 原子完成。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from service_09251_005.domain.models import (
    DispatchOrder,
    HandoffRecord,
    Location,
    OrderStatus,
    RescueRequest,
    RescueTeam,
    RoadClosure,
    ChargingSite,
    iso,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    road_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    km REAL NOT NULL,
    risk INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    phone_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    committed INTEGER NOT NULL,
    assigned_team TEXT,
    next_escalation_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,
    road_id TEXT NOT NULL,
    load INTEGER NOT NULL,
    max_concurrent INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS closures (
    id TEXT PRIMARY KEY,
    road_id TEXT NOT NULL,
    active INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY,
    road_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS compensation_failures (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    at TEXT NOT NULL,
    data TEXT NOT NULL
);
"""


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _phone_key(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


class Repository:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 求援单 ----

    def save_request(self, request: RescueRequest) -> None:
        loc = request.location
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO requests"
                " (id, status, road_id, direction, km, risk, priority, phone_key, created_at, data)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    request.request_id,
                    request.status,
                    loc.road_id,
                    loc.direction,
                    loc.mile_marker_km,
                    int(request.risk),
                    request.priority,
                    _phone_key(request.reporter_phone),
                    iso(request.created_at),
                    _dump(request.to_dict()),
                ),
            )
            self._conn.commit()

    def get_request(self, request_id: str) -> RescueRequest | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM requests WHERE id=?", (request_id,)
            ).fetchone()
        return RescueRequest.from_dict(json.loads(row[0])) if row else None

    def find_merge_candidates(
        self, phone_key: str, loc: Location, radius_km: float
    ) -> list[RescueRequest]:
        """同电话，或同路段同方向且里程相近的未结求援单。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM requests WHERE status='OPEN' AND ("
                " phone_key=?"
                " OR (road_id=? AND direction=? AND km BETWEEN ? AND ?)"
                ")",
                (phone_key, loc.road_id, loc.direction,
                 loc.mile_marker_km - radius_km, loc.mile_marker_km + radius_km),
            ).fetchall()
        return [RescueRequest.from_dict(json.loads(r[0])) for r in rows]

    # ---- 派单 ----

    def save_order(self, order: DispatchOrder) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO orders"
                " (id, request_id, status, priority, committed, assigned_team, next_escalation_at, data)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    order.order_id,
                    order.request_id,
                    order.status.value,
                    order.priority,
                    int(order.committed),
                    order.assigned_team_id,
                    iso(order.next_escalation_at),
                    _dump(order.to_dict()),
                ),
            )
            self._conn.commit()

    def get_order(self, order_id: str) -> DispatchOrder | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM orders WHERE id=?", (order_id,)
            ).fetchone()
        return DispatchOrder.from_dict(json.loads(row[0])) if row else None

    def open_order_for_request(self, request_id: str) -> DispatchOrder | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM orders WHERE request_id=? AND status != 'CLOSED'"
                " ORDER BY rowid LIMIT 1",
                (request_id,),
            ).fetchone()
        return DispatchOrder.from_dict(json.loads(row[0])) if row else None

    def uncommitted_orders_on_road(self, road_id: str) -> list[DispatchOrder]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT o.data FROM orders o JOIN requests r ON o.request_id = r.id"
                " WHERE r.road_id=? AND o.status IN ('PENDING','OFFERED')",
                (road_id,),
            ).fetchall()
        return [DispatchOrder.from_dict(json.loads(r[0])) for r in rows]

    def due_escalations(self, now_iso: str) -> list[DispatchOrder]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM orders"
                " WHERE status IN ('PENDING','OFFERED') AND next_escalation_at <= ?",
                (now_iso,),
            ).fetchall()
        return [DispatchOrder.from_dict(json.loads(r[0])) for r in rows]

    def try_claim_order(self, order: DispatchOrder) -> bool:
        """原子抢单：只有仍处于未承诺状态的派单能被抢走。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE orders SET data=?, status=?, committed=1, assigned_team=?"
                " WHERE id=? AND status IN ('PENDING','OFFERED')",
                (
                    _dump(order.to_dict()),
                    order.status.value,
                    order.assigned_team_id,
                    order.order_id,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def transition_order(self, order: DispatchOrder, expect: tuple[OrderStatus, ...]) -> bool:
        """条件状态迁移，保证并发下状态机不被踩踏。"""
        placeholders = ",".join("?" for _ in expect)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE orders SET data=?, status=?, committed=?, assigned_team=?"
                f" WHERE id=? AND status IN ({placeholders})",
                (
                    _dump(order.to_dict()),
                    order.status.value,
                    int(order.committed),
                    order.assigned_team_id,
                    order.order_id,
                    *[s.value for s in expect],
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    # ---- 救援队与运力 ----

    def save_team(self, team: RescueTeam) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO teams (id, road_id, load, max_concurrent, data)"
                " VALUES (?,?,?,?,?)",
                (
                    team.team_id,
                    team.road_id,
                    team.load,
                    team.max_concurrent,
                    _dump(team.to_dict()),
                ),
            )
            self._conn.commit()

    def get_team(self, team_id: str) -> RescueTeam | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT load, max_concurrent, data FROM teams WHERE id=?", (team_id,)
            ).fetchone()
        if not row:
            return None
        team = RescueTeam.from_dict(json.loads(row[2]))
        team.load = int(row[0])
        team.max_concurrent = int(row[1])
        return team

    def list_teams(self) -> list[RescueTeam]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT load, max_concurrent, data FROM teams"
            ).fetchall()
        teams = []
        for load, max_concurrent, data in rows:
            team = RescueTeam.from_dict(json.loads(data))
            team.load = int(load)
            team.max_concurrent = int(max_concurrent)
            teams.append(team)
        return teams

    def try_reserve_team(self, team_id: str) -> bool:
        """原子占用一份运力；满载时返回 False。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE teams SET load = load + 1 WHERE id=? AND load < max_concurrent",
                (team_id,),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def release_team(self, team_id: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE teams SET load = MAX(load - 1, 0) WHERE id=?", (team_id,)
            )
            self._conn.commit()
            if cur.rowcount != 1:
                raise KeyError(f"team not found: {team_id}")

    # ---- 责任交接链 ----

    def add_handoff(self, handoff: HandoffRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO handoffs (order_id, at, data) VALUES (?,?,?)",
                (handoff.order_id, iso(handoff.at), _dump(handoff.to_dict())),
            )
            self._conn.commit()

    def list_handoffs(self, order_id: str) -> list[HandoffRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM handoffs WHERE order_id=? ORDER BY seq", (order_id,)
            ).fetchall()
        return [HandoffRecord.from_dict(json.loads(r[0])) for r in rows]

    # ---- 道路封闭与补能设施 ----

    def save_closure(self, closure: RoadClosure) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO closures (id, road_id, active, data) VALUES (?,?,?,?)",
                (closure.closure_id, closure.road_id, int(closure.active), _dump(closure.to_dict())),
            )
            self._conn.commit()

    def active_closures(self, road_id: str) -> list[RoadClosure]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM closures WHERE road_id=? AND active=1", (road_id,)
            ).fetchall()
        return [RoadClosure.from_dict(json.loads(r[0])) for r in rows]

    def save_site(self, site: ChargingSite) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sites (id, road_id, data) VALUES (?,?,?)",
                (site.site_id, site.road_id, _dump(site.to_dict())),
            )
            self._conn.commit()

    def get_site(self, site_id: str) -> ChargingSite | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM sites WHERE id=?", (site_id,)
            ).fetchone()
        return ChargingSite.from_dict(json.loads(row[0])) if row else None

    def list_sites(self, road_id: str) -> list[ChargingSite]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM sites WHERE road_id=?", (road_id,)
            ).fetchall()
        return [ChargingSite.from_dict(json.loads(r[0])) for r in rows]

    # ---- 审计与补偿失败台账 ----

    def record_event(self, kind: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (kind, at, data) VALUES (?,?,?)",
                (kind, iso(datetime.now(timezone.utc)), _dump(payload)),
            )
            self._conn.commit()

    def list_events(self, kind: str | None = None) -> list[dict]:
        with self._lock:
            if kind is None:
                rows = self._conn.execute(
                    "SELECT kind, data FROM events ORDER BY seq"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT kind, data FROM events WHERE kind=? ORDER BY seq", (kind,)
                ).fetchall()
        return [{"kind": k, **json.loads(d)} for k, d in rows]

    def record_compensation_failure(self, order_id: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO compensation_failures (order_id, at, data) VALUES (?,?,?)",
                (order_id, iso(datetime.now(timezone.utc)), _dump(payload)),
            )
            self._conn.commit()

    def list_compensation_failures(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT order_id, data FROM compensation_failures ORDER BY seq"
            ).fetchall()
        return [{"order_id": oid, **json.loads(d)} for oid, d in rows]
