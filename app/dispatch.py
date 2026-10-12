"""后台读取需求、进程内去重及后续业务流程入口。"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from app.config import Settings, load_fields
from app.feishu import FeishuClient

logger = logging.getLogger(__name__)


def process_record(record_id: str, mapping: dict[str, str], feishu: FeishuClient) -> None:
    """功能说明：读取和校验需求。参数 record_id：记录 ID；mapping：本次字段快照；feishu：飞书客户端。返回值：无。"""
    fields = feishu.get_record(record_id)
    demand = {key: fields.get(name) for key, name in mapping.items()}
    missing = [mapping[key] for key in ("title", "description") if not demand.get(key)]
    if missing:
        raise ValueError("需求信息不完整，缺少：" + "、".join(missing))
    logger.info("需求读取完成：record_id=%s mapped_fields=%d stage=record_loaded",
                record_id, len(demand))
    # AI 评级和人员分配在此接入；当前入口只读取并校验需求。


class Dispatcher:
    def __init__(self, settings: Settings, runner: Callable = process_record):
        """功能说明：初始化单线程处理器。参数 settings：运行配置；runner：单条记录处理函数。返回值：无。"""
        self.settings = settings
        self.feishu = FeishuClient(settings)
        self.runner = runner
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dispatch")
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._recent: dict[str, float] = {}

    def submit(self, record_id: str) -> bool:
        """功能说明：校验配置并提交后台任务。参数 record_id：记录 ID。返回值：新任务为真，重复请求为假。"""
        mapping = load_fields(self.settings.fields_path)
        with self._lock:
            now = time.monotonic()
            self._recent = {key: expiry for key, expiry in self._recent.items() if expiry > now}
            if record_id in self._active or record_id in self._recent:
                logger.info("跳过重复请求：record_id=%s", record_id)
                return False
            if len(self._active) >= 100:
                raise RuntimeError("后台任务已满，请稍后重试")
            self._active.add(record_id)
            try:
                self._executor.submit(self._run, record_id, mapping)
            except RuntimeError:
                self._active.remove(record_id)
                raise
        logger.info("任务已接收：record_id=%s", record_id)
        return True

    def _run(self, record_id: str, mapping: dict[str, str]) -> None:
        """执行任务；失败允许再次投递，成功保留五分钟去重记录。"""
        success = False
        logger.info("开始处理：record_id=%s", record_id)
        try:
            self.runner(record_id, mapping, self.feishu)
            success = True
        except Exception:
            logger.exception("后台处理失败：record_id=%s，可重新发送请求", record_id)
        finally:
            with self._lock:
                self._active.discard(record_id)
                if success:
                    self._recent[record_id] = time.monotonic() + 300
            logger.info("处理结束：record_id=%s success=%s", record_id, success)

    def close(self) -> None:
        """停止接收后台任务并等待已接收任务结束。"""
        self._executor.shutdown(wait=True)
