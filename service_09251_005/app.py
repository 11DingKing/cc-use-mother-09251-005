"""组合根：装配仓储、端口与应用服务。

运行数据目录优先取 RESCUE_DATA_DIR 环境变量，默认落在系统临时目录，
绝不写入源码目录。启动时自动恢复超时升级扫描。
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from service_09251_005.application.ports import (
    Clock,
    IdGenerator,
    ListNotifier,
    Notifier,
    SystemClock,
    UuidIds,
)
from service_09251_005.application.privacy import PRIVACY_SCOPE, WRITE_SCOPE, Authorizer
from service_09251_005.application.services import (
    AdminService,
    CapacityGateway,
    DispatchConfig,
    DispatchService,
    IntakeService,
    OperationService,
)
from service_09251_005.infrastructure.repository import Repository

DEFAULT_TOKENS = {
    "dispatcher-token": {WRITE_SCOPE, PRIVACY_SCOPE},
    "team-token": {WRITE_SCOPE},
}


@dataclass
class App:
    repo: Repository
    clock: Clock
    idgen: IdGenerator
    notifier: Notifier
    authorizer: Authorizer
    capacity: CapacityGateway
    config: DispatchConfig
    intake: IntakeService
    dispatch: DispatchService
    ops: OperationService
    admin: AdminService
    data_dir: str

    def close(self) -> None:
        self.repo.close()


def build_app(
    data_dir: str | None = None,
    *,
    clock: Clock | None = None,
    idgen: IdGenerator | None = None,
    notifier: Notifier | None = None,
    tokens: dict[str, set[str]] | None = None,
    config: DispatchConfig | None = None,
    recover: bool = True,
) -> App:
    data_dir = data_dir or os.environ.get("RESCUE_DATA_DIR")
    if not data_dir:
        data_dir = tempfile.mkdtemp(prefix="rescue-dispatch-")
    os.makedirs(data_dir, exist_ok=True)

    repo = Repository(os.path.join(data_dir, "rescue.db"))
    clock = clock or SystemClock()
    idgen = idgen or UuidIds()
    notifier = notifier or ListNotifier()
    cfg = config or DispatchConfig()
    capacity = CapacityGateway(repo)
    authorizer = Authorizer(tokens or DEFAULT_TOKENS)

    dispatch = DispatchService(repo, clock, idgen, notifier, cfg)
    intake = IntakeService(repo, clock, idgen, dispatch, notifier, cfg)
    ops = OperationService(repo, clock, idgen, notifier, capacity, dispatch)
    admin = AdminService(repo, clock, idgen)

    app = App(
        repo=repo, clock=clock, idgen=idgen, notifier=notifier,
        authorizer=authorizer, capacity=capacity, config=cfg,
        intake=intake, dispatch=dispatch, ops=ops, admin=admin,
        data_dir=data_dir,
    )
    if recover:
        # 重启后继续升级：超期未承诺的派单在库中持久化，启动即扫描
        dispatch.recover()
    return app
