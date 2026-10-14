"""验证任务日志边界、异常摘要与访问过滤。"""

import logging
import re
import threading
from unittest.mock import Mock

from app.dispatch import Dispatcher
from app.task_logging import AccessErrorsOnly, TaskFormatter, task_context, task_details


def test_task_blocks_and_context_cleanup(caplog):
    """连续成功与失败任务各自成块，失败只记录一次堆栈且不污染下一任务。"""
    dispatcher = object.__new__(Dispatcher)
    dispatcher.feishu = Mock()
    dispatcher._lock = threading.Lock()
    dispatcher._active = set()
    dispatcher._recent = {}

    def runner(record_id, mapping, feishu):
        """模拟成功回写和外部读取失败。"""
        assert task_context.get()["stage"] == "开始处理"
        if record_id == "recFail":
            task_details(stage="读取记录")
            raise RuntimeError("HTTP=400 code=1254607 尝试=3/3")
        task_details(outcome="回写成功")

    dispatcher.runner = runner
    with caplog.at_level(logging.INFO):
        for record_id in ("recFirst", "recFail", "recLast"):
            dispatcher._run(record_id, {})
            assert task_context.get() is None
    formatter = TaskFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    lines = [formatter.format(record) for record in caplog.records]
    assert sum(line == "" for line in lines) == 3
    assert sum("Traceback" in line for line in lines) == 1
    assert sum("任务完成" in line for line in lines) == 2
    assert "阶段=读取记录" in '\n'.join(lines)
    for record_id in ("recFirst", "recFail", "recLast"):
        assert sum(record_id in line for line in lines) == 1
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO .*任务开始", lines[0])
    assert "recFail" not in dispatcher._recent


def test_http_success_silent_and_errors_visible():
    """健康检查、正常接收请求静默，鉴权与服务错误保留。"""
    filt = AccessErrorsOnly()
    for status in (200, 202, 401, 500):
        record = logging.LogRecord("gunicorn.access", logging.INFO, "", 0, "%(s)s", ({"s": str(status)},), None)
        assert filt.filter(record) is (status >= 400)
