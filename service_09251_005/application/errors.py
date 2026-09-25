"""应用层错误类型：接口边界据此映射 HTTP 状态码。"""
from __future__ import annotations


class DomainError(Exception):
    code = "DOMAIN_ERROR"

    def __init__(self, message: str = "", **details: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details


class ValidationError(DomainError):
    code = "VALIDATION"


class NotFoundError(DomainError):
    code = "NOT_FOUND"


class AuthorizationError(DomainError):
    code = "FORBIDDEN"


class ConflictError(DomainError):
    code = "CONFLICT"


class ClaimConflictError(ConflictError):
    code = "CLAIM_CONFLICT"


class CapacityError(ConflictError):
    code = "CAPACITY_EXHAUSTED"


class TransferFailedError(DomainError):
    """转派失败且补偿已成功回滚，状态保持一致。"""

    code = "TRANSFER_FAILED"


class CompensationFailedError(DomainError):
    """补偿动作本身失败：已记录待人工处理的悬挂状态。"""

    code = "COMPENSATION_FAILED"
