"""任务摘要上下文、控制台与文件日志。"""

from contextvars import ContextVar
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path


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


class TaskFileHandler(RotatingFileHandler):
    """轮转后创建的新日志同样使用 0640 权限。"""

    def _open(self):
        """以受限权限打开 UTF-8 日志文件。"""
        return open(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors,
                    opener=lambda path, flags: os.open(path, flags, 0o640))


def configure_logging():
    """功能说明：配置控制台、轮转文件日志和访问过滤。参数：无。返回值：无。"""
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    root.setLevel(logging.INFO)
    formatter = TaskFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for handler in root.handlers:
        handler.setFormatter(formatter)
    directory = Path(os.getenv("LOG_DIR", "logs")).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    target = directory / "app.log"
    file_handler = next((h for h in root.handlers if getattr(h, "_task_log_file", False)
                         and h.baseFilename == str(target)), None)
    for handler in list(root.handlers):
        if getattr(handler, "_task_log_file", False) and handler is not file_handler:
            root.removeHandler(handler)
            for name in ("gunicorn.access", "gunicorn.error"):
                logging.getLogger(name).removeHandler(handler)
            handler.close()
    if file_handler is None:
        file_handler = TaskFileHandler(target, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file_handler._task_log_file = True
        root.addHandler(file_handler)
    file_handler.setFormatter(formatter)
    target.chmod(0o640)
    # Gunicorn 工作进程的日志默认不向根记录器传播，单独接入同一个文件处理器。
    for name in ("gunicorn.access", "gunicorn.error"):
        gunicorn_logger = logging.getLogger(name)
        if not gunicorn_logger.propagate and file_handler not in gunicorn_logger.handlers:
            gunicorn_logger.addHandler(file_handler)
    access = logging.getLogger("gunicorn.access")
    if not any(isinstance(item, AccessErrorsOnly) for item in access.filters):
        access.addFilter(AccessErrorsOnly())
