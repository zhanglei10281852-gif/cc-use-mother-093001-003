"""服务层错误类型：参数校验、对象缺失、状态冲突。"""


class OverflowError(Exception):
    """服务基础错误。"""


class ValidationError(OverflowError):
    """输入不满足契约（缺字段、时区缺失、载荷不合规等）。"""


class NotFoundError(OverflowError):
    """引用的事件、证据、报告或整改项不存在。"""


class ConflictError(OverflowError):
    """与既有状态冲突（重复编号、重复确认、重复撤回等）。"""
