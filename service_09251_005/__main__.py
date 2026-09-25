"""服务启动引导：加载种子资源、恢复快照、启动 HTTP 与后台 tick。

运行数据（快照）路径由 --snapshot 或环境变量 RESCUE_SNAPSHOT 指定，
默认放在系统临时目录，绝不写入源码目录。

用法：
    python3 -m service_09251_005 \
        --seed seed.json --snapshot /var/lib/rescue/state.json --port 8080
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
from datetime import datetime

from .application import Repository, RescueService
from .clock import SystemClock
from .domain.enums import Capability
from .domain.station import ChargingStation
from .domain.team import DutyWindow
from .interfaces import JsonApi, serve


def bootstrap(seed_path: str | None, snapshot_path: str | None,
              tick_seconds: int = 10) -> tuple[RescueService, JsonApi,
                                                threading.Thread | None]:
    clock = SystemClock()
    repo = Repository(snapshot_path)
    repo.load()
    svc = RescueService(repo, clock)

    # 仅在空库时装载种子（节点/道路/服务区/队伍），避免重启覆盖运行态
    if seed_path and not repo.nodes_seeded():
        with open(seed_path, encoding="utf-8") as f:
            seed = json.load(f)
        for n in seed.get("nodes", []):
            svc.add_node(n["node_id"], n.get("name", ""))
        for g in seed.get("segments", []):
            svc.add_segment(
                g["segment_id"], g["a"], g["b"], float(g["distance_km"]),
                direction=g.get("direction", "bidirectional"),
                speed_kmh=float(g.get("speed_kmh", 80.0)),
            )
        for s in seed.get("stations", []):
            svc.register_station(ChargingStation(
                station_id=s["station_id"], name=s["name"],
                node_id=s["node_id"], total_ports=int(s["total_ports"]),
                occupied=int(s.get("occupied", 0)),
                queue_length=int(s.get("queue_length", 0)),
                avg_charge_minutes=float(s.get("avg_charge_minutes", 40.0)),
                offline=bool(s.get("offline", False)),
            ))
        for t in seed.get("teams", []):
            wins = [
                DutyWindow(
                    start_local=w["start"], end_local=w["end"],
                    weekdays=(
                        DutyWindow.weekdays_from_names(w["weekdays"])
                        if w.get("weekdays") else frozenset(range(7))
                    ),
                    tz_name=w.get("tz", "Asia/Shanghai"),
                )
                for w in t.get("duty_windows", [])
            ]
            svc.register_team(
                t["team_id"], t["name"], t["org_id"], t["base_node"],
                {Capability(c) for c in t["capabilities"]},
                duty_windows=wins,
                mobile_power_kwh=float(t.get("mobile_power_kwh", 0.0)),
            )
        repo.save()

    api = JsonApi(svc)

    ticker = None
    if tick_seconds > 0:
        stop = threading.Event()

        def _loop() -> None:
            while not stop.wait(tick_seconds):
                try:
                    svc.run_due()
                except Exception as exc:  # 单次 tick 失败不影响循环
                    print(f"[tick] {datetime.now().isoformat()} error: {exc}")

        ticker = threading.Thread(target=_loop, name="rescue-tick",
                                  daemon=True)
        ticker.stop_event = stop  # type: ignore[attr-defined]
        ticker.start()
    return svc, api, ticker


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="新能源道路救援派单服务")
    ap.add_argument("--seed", help="种子资源 JSON（节点/道路/服务区/队伍）")
    ap.add_argument(
        "--snapshot",
        default=os.environ.get(
            "RESCUE_SNAPSHOT",
            os.path.join(tempfile.gettempdir(), "rescue_dispatch_state.json"),
        ),
        help="运行态快照路径（默认取 RESCUE_SNAPSHOT 或临时目录）",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--tick-seconds", type=int, default=10,
                    help="后台驱动周期；0 表示不启用")
    args = ap.parse_args(argv)

    _svc, api, ticker = bootstrap(args.seed, args.snapshot, args.tick_seconds)
    print(f"救援派单服务启动: http://{args.host}:{args.port} "
          f"快照={args.snapshot}")
    try:
        serve(api, args.host, args.port)
    except KeyboardInterrupt:
        pass
    finally:
        if ticker is not None:
            ticker.stop_event.set()  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
