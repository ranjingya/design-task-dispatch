"""任务摘要上下文与控制台日志格式。"""

from contextvars import ContextVar
import logging


task_context = ContextVar("task_context", default=None)


def task_details(**values):
    """记录当前任务的阶段、推荐人和结果；独立调用业务函数时不保留上下文。"""
    current = task_context.get()
    if current is not None:
        current.update(values)


class TaskFormatter(logging.Formatter):
    """任务之间输出空行，其余日志统一带日期、时间和级别。"""

    def format(self, record):
        """格式化日志记录，任务分隔标记返回空行。"""
        if getattr(record, "task_separator", False):
            return ""
        return super().format(record)


class AccessErrorsOnly(logging.Filter):
    """成功 HTTP 请求由业务摘要覆盖，只保留异常访问。"""

    def filter(self, record):
        """检查 Gunicorn 状态码；未知格式保留以便排查。"""
        try:
            return int(record.args["s"]) >= 400
        except (KeyError, TypeError, ValueError):
            return True


def configure_logging():
    """功能说明：配置摘要日志和访问过滤。参数：无。返回值：无。"""
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    root.setLevel(logging.INFO)
    formatter = TaskFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for handler in root.handlers:
        handler.setFormatter(formatter)
    access = logging.getLogger("gunicorn.access")
    if not any(isinstance(item, AccessErrorsOnly) for item in access.filters):
        access.addFilter(AccessErrorsOnly())
