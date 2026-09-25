"""领域与应用层错误。API 边界据此映射错误码。"""
from __future__ import annotations


class DispatchError(Exception):
    code = "dispatch_error"


class NotFoundError(DispatchError):
    code = "not_found"


class ConflictError(DispatchError):
    """状态不允许该操作，例如工单已被承诺。"""

    code = "conflict"


class AlreadyClaimedError(ConflictError):
    code = "already_claimed"


class NoFeasibleTeamError(DispatchError):
    code = "no_feasible_team"


class TransferRejectedError(DispatchError):
    """转派前置条件不满足（责任不得悬空）。"""

    code = "transfer_rejected"


class CompensationError(DispatchError):
    """外部资源补偿（释放充电位预约）失败，需要进入台账重试。"""

    code = "compensation_failed"


class StationFullError(DispatchError):
    code = "station_full"


class PrivacyDeniedError(DispatchError):
    code = "privacy_denied"
