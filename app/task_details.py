"""将最终负责人的任务拆成明细，供飞书月度透视统计。"""

import json
import logging
import re
import threading
from urllib.parse import quote

from app.config import load_fields
from app.feishu import FeishuClient
from app.notifications import deadline_date
from app.recommendation import checked, user_ids
from app.rules import load_score, plain

logger = logging.getLogger(__name__)
DETAIL_TYPES = {"title": 1, "person": 11, "month": 1, "score": 2, "url": 15, "key": 1}
SOURCE_TYPES = {"title": 1, "assignee": 11, "deadline": 5, "load_score": 2, "cancelled": 7}
DETAIL_KEY = re.compile(r"rec[A-Za-z0-9]{1,125}:ou_[A-Za-z0-9]{1,125}")


def load_config(settings):
    """读取容器外明细字段映射与轮询间隔。"""
    config = json.loads((settings.fields_path.parent / "task_details.json").read_text(encoding="utf-8"))
    mapping = config.get("fields", {})
    if not isinstance(mapping, dict) or any(not isinstance(mapping.get(k), str) or not mapping[k].strip()
                                            for k in DETAIL_TYPES):
        raise ValueError("人员任务明细字段配置不完整")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("人员任务明细字段映射不可重复")
    interval = config.get("interval_seconds")
    if type(interval) not in (int, float) or not 10 <= interval <= 86400:
        raise ValueError("明细同步间隔需在 10 至 86400 秒之间")
    return mapping, interval


def validate_fields(schema, mapping, types):
    """同步前核验字段名称和类型，防止字段删除或误配置造成明细丢失。"""
    actual = {f["field_name"]: f for f in schema}
    if any(k not in mapping or actual.get(mapping[k], {}).get("type") != t for k, t in types.items()):
        raise ValueError("明细同步字段缺失或类型不符，保留现有明细")


def desired_details(records, mapping, settings):
    """功能说明：按任务与最终负责人生成唯一明细。参数 records：完整需求快照；mapping：需求字段映射；settings：Base 与需求表参数。返回值：明细键到标准字段值的字典；无负责人、无截止日期或已取消的任务不生成明细。"""
    result, seen = {}, set()
    base, table = quote(settings.base_token, safe=""), quote(settings.table_id, safe="")
    for record in records:
        rid, fields = record["record_id"], record["fields"]
        if not re.fullmatch(r"rec[A-Za-z0-9]{1,125}", rid) or rid in seen or not isinstance(fields, dict):
            raise ValueError("需求快照含无效或重复记录，保留现有明细")
        seen.add(rid)
        if checked(fields.get(mapping["cancelled"])):
            continue
        people = fields.get(mapping["assignee"]) or []
        if not isinstance(people, list) or any(not isinstance(p, dict) or not isinstance(p.get("id"), str)
            or not re.fullmatch(r"ou_[A-Za-z0-9]{1,125}", p["id"]) for p in people):
            raise ValueError("最终负责人数据不完整，保留现有明细")
        due = deadline_date(fields.get(mapping["deadline"]))
        if due is None:
            continue
        for uid in dict.fromkeys(user_ids(people)):
            key = f"{rid}:{uid}"
            result[key] = {
                "title": plain(fields.get(mapping["title"])),
                "person": [{"id": uid}], "month": due.strftime("%Y-%m"),
                "score": load_score(fields.get(mapping["load_score"])),
                "url": {"text": "查看需求", "link": f"https://feishu.cn/base/{base}?table={table}&record={rid}"},
                "key": key,
            }
    return result


def same_detail(fields, values, mapping):
    """忽略接口附加的人员显示名，比较程序维护的字段。"""
    score = fields.get(mapping["score"])
    return (all(plain(fields.get(mapping[k])) == values[k] for k in ("title", "month", "key"))
            and user_ids(fields.get(mapping["person"])) == user_ids(values["person"])
            and (score is None or score == "" or load_score(score) is not None)
            and load_score(score) == values["score"]
            and fields.get(mapping["url"]) == values["url"])


def sync_details(feishu):
    """功能说明：完整读取并对账人员明细，仅写派生表。参数 feishu：数据应用客户端。返回值：新增、更新、删除数量；读取或校验失败时停止，未知结果由下一次轮询重新对账。"""
    settings = feishu.settings
    target = settings.task_details_table_id
    if not target or target == settings.table_id:
        raise ValueError("请配置独立的 FEISHU_TASK_DETAILS_TABLE_ID")
    mapping, _ = load_config(settings)
    source = load_fields(settings.fields_path)
    validate_fields(feishu.get_fields(), source, SOURCE_TYPES)
    validate_fields(feishu.get_fields(target), mapping, DETAIL_TYPES)
    desired = desired_details(feishu.get_records(settings.table_id), source, settings)
    current = feishu.get_records(target)
    create, update, delete, seen = [], [], [], set()
    for record in current:
        rid, fields = record["record_id"], record["fields"]
        key = plain(fields.get(mapping["key"]))
        if not DETAIL_KEY.fullmatch(key):
            raise ValueError("明细表存在非程序记录，请先检查；本轮保留全部明细")
        if key not in desired or key in seen:
            delete.append(rid)
            continue
        seen.add(key)
        if not same_detail(fields, desired[key], mapping):
            update.append({"record_id": rid, "fields": {mapping[k]: v for k, v in desired[key].items()}})
    create = [{"fields": {mapping[k]: v for k, v in values.items()}} for key, values in desired.items() if key not in seen]
    # 先补齐目标，再清理过期明细；失败响应不自动重放创建请求。
    for action, records in (("create", create), ("update", update), ("delete", delete)):
        if records:
            feishu.batch_records(target, action, records)
    counts = {"created": len(create), "updated": len(update), "deleted": len(delete)}
    if any(counts.values()):
        logger.info("人员明细同步完成 新增=%d 更新=%d 删除=%d", *counts.values())
    return counts


class TaskDetailsSync:
    """独立后台轮询，不占用 AI 队列，也不发送任务通知。"""

    def __init__(self, settings):
        """功能说明：启动独立明细同步线程。参数 settings：应用与表配置。返回值：无；服务退出时由 close 停止。"""
        self.feishu = FeishuClient(settings)
        self.interval = 60
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="task-details-sync", daemon=True)
        self._thread.start()
        logger.info("人员明细定时同步已启动")

    def _run(self):
        """启动时立即同步；外部故障记录后等待下一轮重试。"""
        while not self._stop.is_set():
            try:
                self.interval = load_config(self.feishu.settings)[1]
                sync_details(self.feishu)
            except Exception:
                logger.exception("人员明细同步失败，等待下一轮重试")
            if self._stop.wait(self.interval):
                break

    def close(self):
        """停止轮询，正在进行的 HTTP 请求由客户端超时收尾。"""
        self._stop.set()
        self._thread.join(timeout=2)
