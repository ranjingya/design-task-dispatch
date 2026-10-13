"""后台读取需求、进程内去重及后续业务流程入口。"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from app.ai import AIClient
from app.materials import collect_images
from app.config import Settings, load_fields
from app.feishu import FeishuClient
from app.rules import select_department, review, format_result, load_score
from app.rule_cache import load_daily_rules
from app.recommendation import protected, recommend, save_rotation

logger = logging.getLogger(__name__)


def process_record(record_id: str, mapping: dict[str, str], feishu: FeishuClient) -> None:
    """功能说明：读取需求、调用 AI 评级、按规则推荐人员并回写结果。参数 record_id：记录 ID；mapping：本次字段快照；feishu：飞书客户端。返回值：无。"""
    schema = {item["field_name"]: item for item in feishu.get_fields()}
    result_name = mapping.get("rule_explanation")
    type_name, level_name = mapping.get("work_type"), mapping.get("level")
    if schema.get(result_name, {}).get("type") != 1 or any(
        schema.get(name, {}).get("type") != 3 for name in (type_name, level_name)
    ):
        raise ValueError("匹配结果需为文本，工作类型和分类等级需为单选，请核对字段映射")
    score_name = mapping.get("load_score")
    if schema.get(score_name, {}).get("type") != 2:
        raise ValueError("任务负载分需为数字字段，请核对字段映射")
    if schema.get(mapping.get("department"), {}).get("type") != 3 or schema.get(
        mapping.get("platform"), {}
    ).get("type") != 1:
        raise ValueError("部门需为单选，平台需为文本，请核对字段映射")
    recommending = "recommendation_result" in mapping
    if recommending and (schema.get(mapping['recommendation_result'], {}).get('type') != 1 or
                         schema.get(mapping['recommended_assignee'], {}).get('type') != 11):
        raise ValueError("推荐结果需为文本，系统推荐人需为人员字段")
    try:
        feishu.settings.validate_ai()
        fields = feishu.get_record(record_id)
        if recommending and protected(fields, mapping):
            logger.info("保留人工安排或已结束任务：record_id=%s", record_id)
            return
        demand = {key: fields.get(mapping[key]) for key in
                  ("title", "description", "department", "platform", "deadline", "notes") if key in mapping}
        rules = load_daily_rules(feishu)
        department, eligible = select_department(demand.get("department"), rules)
        options = [item["name"] for item in schema[type_name].get("property", {}).get("options", [])]
        levels = {item["name"] for item in schema[level_name].get("property", {}).get("options", [])}
        if not options or not {"A", "B", "C"} <= levels:
            raise ValueError("工作类型或分类等级选项配置不完整")
        missing = [mapping[key] for key in ("title", "description") if not demand.get(key)]
        logger.info("需求读取完成：record_id=%s field_count=%d", record_id, len(demand))
        if missing:
            result = review("需求信息不完整", missing, department)
        else:
            attachment_name = mapping.get("attachments")
            attachment_field = schema.get(attachment_name, {})
            if attachment_name and attachment_field.get("type") != 17:
                raise ValueError("附件字段映射无效")
            images, issues = collect_images(fields.get(attachment_name) or [], record_id,
                                            attachment_field.get("field_id", ""), feishu)
            # 未读取的材料显式传入，让模型列出候选；程序禁止此时给出确定评级。
            demand["material_issues"] = issues
            result = AIClient(feishu.settings).classify(demand, eligible, options, images, department)
        score = None
        rule = None
        explanation = format_result(result, rules)
        if result["matched"]:
            rule = next(r for r in rules if r["record_id"] == result["rule_id"])
            score = load_score(rule.get("load_score"))
            if score is None:
                explanation += "\n负载分：规则分数缺失或无效，待补充后重新处理。"
                logger.warning("命中规则缺少有效负载分：record_id=%s rule_id=%s", record_id, rule["record_id"])
        updates = {result_name: explanation, type_name: result["work_type"],
                   level_name: result["level"], score_name: score}
        pending = None
        if recommending:
            try:
                recommendation, pending = recommend(record_id, fields, demand, rule, mapping, feishu)
                updates.update(recommendation)
            except Exception:
                logger.exception("人员推荐失败：record_id=%s", record_id)
                updates.update({mapping['recommendation_result']: "推荐失败：资源读取或配置校验失败，请查看日志并重新触发。",
                                mapping['recommended_assignee']: []})
            # 模型运行期间可能有人确认，回写前再次检查人工安排。
            if protected(feishu.get_record(record_id), mapping):
                logger.info("回写前发现人工安排，保留当前结果：record_id=%s", record_id)
                return
        feishu.update_record(record_id, updates)
        try:
            save_rotation(pending)
        except Exception:
            logger.exception("轮换位置保存失败，推荐已回写：record_id=%s", record_id)
        logger.info("评级回写完成：record_id=%s matched=%s load_score=%s", record_id, result["matched"], score)
    except Exception:
        logger.exception("评级流程失败：record_id=%s", record_id)
        try:
            feishu.update_record(record_id, {result_name: "处理失败：读取材料、调用模型或校验结果失败，请查看服务日志并重新触发。"})
        except Exception:
            logger.exception("失败提示回写失败：record_id=%s", record_id)
        raise


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
