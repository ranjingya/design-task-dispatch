"""需求流程通知：读取容器外配置，使用独立应用机器人私聊并持久化去重。"""

import fcntl
import hashlib
import html
import json
import logging
import os
import re
import time
import tomllib
from dataclasses import replace
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.feishu import FeishuClient
from app.rules import plain

logger = logging.getLogger(__name__)
RECORD_NOTIFICATION_EVENTS = frozenset({"leader_confirmed", "assignee_confirmed", "deadline_reminder"})


def load_notifications(path, event="new_task"):
    """功能说明：读取通知配置。参数 path：TOML 路径；event：通知事件。返回值：通过校验的事件配置，未启用时返回空字典。"""
    if not path.exists():
        logger.debug("新增通知未配置")
        return {}
    config = tomllib.loads(path.read_text(encoding="utf-8")).get(event, {})
    if not isinstance(config, dict) or type(config.get("enabled", False)) is not bool:
        raise ValueError(f"{event}.enabled 必须是布尔值")
    if not config.get("enabled", False):
        return {}
    people = config.get("recipients", [])
    if not isinstance(people, list):
        raise ValueError(f"{event}.recipients 必须为人员列表")
    seen = set()
    for person in people:
        if not isinstance(person, dict) or type(person.get("enabled")) is not bool:
            raise ValueError("每个通知人员必须配置 enabled 布尔值")
        uid = person.get("open_id", "")
        if not isinstance(uid, str) or not re.fullmatch(r"ou_[A-Za-z0-9]{1,125}", uid):
            raise ValueError("通知人员需填写通知应用下的 open_id（ou_ 开头）")
        if not isinstance(person.get("name"), str) or not person["name"].strip():
            raise ValueError("通知人员需填写 name")
        if uid in seen:
            raise ValueError("通知人员 open_id 不可重复")
        seen.add(uid)
    return config


def escape_text(value):
    """将用户输入作为文字显示，避免被解释为卡片标签或链接。"""
    text = html.escape(plain(value)[:2000], quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!~|>-])", r"\\\1", text)


def notification_content(record_id, fields, mapping, settings, feishu, event="new_task"):
    """功能说明：按通知事件生成卡片内容和标题颜色。参数 record_id：记录 ID；fields：回写后字段；mapping：字段映射；settings：数据配置；feishu：人员身份查询客户端；event：通知事件。返回值：飞书 Card 2.0 内容。"""
    def value(key):
        return fields.get(mapping.get(key))

    bot = FeishuClient(replace(settings, app_id=settings.alert_app_id, app_secret=settings.alert_app_secret))
    resolved = {}

    def people(key):
        result = []
        for person in value(key) or []:
            if not isinstance(person, dict) or not person.get("name"):
                raise ValueError("通知人员显示名称缺失，待重新读取记录")
            # 卡片引用需使用通知应用的 Open ID，通过 Union ID 关联同一人员。
            try:
                source_id = person["id"]
                if source_id not in resolved:
                    resolved[source_id] = alert_open_id(source_id, feishu, bot)
                uid = resolved[source_id]
                result.append(f'<at id="{uid}"></at>')
            except Exception:
                logger.warning("通知人员引用不可用，使用姓名展示")
                result.append(escape_text(person["name"]))
        return " ".join(result)

    if event == "new_task" and (not value("rule_explanation") or not value("recommendation_result")):
        raise ValueError("匹配与推荐结果尚未就绪，暂不发送通知")
    if event == "new_task" and str(value("rule_explanation")).startswith("处理失败"):
        raise ValueError("任务处理失败，暂不发送新增通知")
    lines = []
    if event == "new_task":
        lines = [
            ("任务名称", escape_text(value("title")) or "未命名需求"),
            ("需求说明", escape_text(value("description")) or "未填写"),
            ("需求人员", people("submitter") or "未填写"),
            ("推荐负责人", people("recommended_assignee") or "待人工分配"),
            ("所属平台", escape_text(value("platform")) or "未填写"),
        ]
    title = "美工任务上新 · 组长确认"
    if event == "leader_confirmed":
        title = "美工任务上新 · 已分配"
        lines = [("任务名称", escape_text(value("title")) or "未命名需求"),
                 ("需求说明", escape_text(value("description")) or "未填写"),
                 ("所属平台", escape_text(value("platform")) or "未填写"),
                 ("需求人员", people("submitter") or "未填写"),
                 ("最终负责人", people("assignee"))]
    elif event == "assignee_confirmed":
        title = "美工任务 · 负责人已接收确认"
        lines = [("任务名称", escape_text(value("title")) or "未命名需求"),
                 ("最终负责人", people("assignee"))]
    elif event == "deadline_reminder":
        title = "美工任务 · 截止日期提醒"
        due = deadline_date(value("deadline"))
        lines = [("任务名称", escape_text(value("title")) or "未命名需求"),
                 ("需求人员", people("submitter") or "未填写"),
                 ("所属平台", escape_text(value("platform")) or "未填写"),
                 ("截止日期", due.isoformat() if due is not None else "未填写")]
    base = quote(settings.base_token, safe="")
    table = quote(settings.table_id, safe="")
    record = quote(record_id, safe="")
    source_url = f"https://feishu.cn/base/{base}?table={table}"
    text = "\n".join(f"**{label}：** {text}" for label, text in lines)
    elements = [
        {"tag": "markdown", "content": text},
        {"tag": "button", "text": {"tag": "plain_text", "content": "查看需求"}, "type": "default",
         "behaviors": [{"type": "open_url", "default_url": source_url + f"&record={record}"}]},
    ]
    return {"schema": "2.0", "config": {"width_mode": "default"},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "template": "red" if event == "deadline_reminder" else "purple"},
            "body": {"direction": "vertical", "vertical_spacing": "12px", "elements": elements}}


def send_new_task(record_id, mapping, feishu):
    """功能说明：读取最新结果，逐人发送一次新增通知；失败可单独补发。参数 record_id：需求记录 ID；mapping：字段映射；feishu：数据应用客户端。返回值：所有启用接收人发送成功或已发送时为真，存在失败时为假。"""
    settings = feishu.settings
    config = load_notifications(settings.fields_path.parent / "notifications.toml")
    recipients = [p for p in config.get("recipients", []) if p["enabled"]]
    if not recipients:
        logger.debug("新增通知跳过：未启用接收人")
        return True
    return deliver(record_id, mapping, feishu, recipients, "new_task")


def alert_open_id(source_id, feishu, bot):
    """功能说明：解析通知应用下的人员身份。参数 source_id：数据应用 Open ID；feishu：数据客户端；bot：通知客户端。返回值：通知应用 Open ID。"""
    if feishu.settings.app_id == bot.settings.app_id:
        uid = source_id
    else:
        union_id = feishu.get_contact_user(source_id).get("union_id")
        if not isinstance(union_id, str) or not union_id.startswith("on_"):
            raise ValueError("人员 Union ID 不可用")
        uid = bot.get_contact_user_by_union_id(union_id).get("open_id", "")
    if not isinstance(uid, str) or not re.fullmatch(r"ou_[A-Za-z0-9]{1,125}", uid):
        raise ValueError("通知应用 Open ID 不可用")
    return uid


def deadline_date(value):
    """将飞书日期字段的毫秒时间戳转换为服务时区的日期，空值返回空。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("截止时间必须是有效的毫秒时间戳")
    try:
        return datetime.fromtimestamp(float(value) / 1000, ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))).date()
    except (TypeError, ValueError, OverflowError, OSError):
        raise ValueError("截止时间必须是有效的毫秒时间戳") from None


def notify_record(record_id, event, mapping, feishu):
    """功能说明：校验最新状态并发送确认或截止提醒，不修改任务或调用 AI。参数 record_id：需求 ID；event：通知事件；mapping：字段映射；feishu：数据客户端。返回值：sent_or_duplicate、skipped 或 disabled；发送失败抛出异常。"""
    if event not in RECORD_NOTIFICATION_EVENTS:
        raise ValueError("不支持的通知事件")
    settings = feishu.settings
    config = load_notifications(settings.fields_path.parent / "notifications.toml", event)
    if not config:
        return "disabled"
    fields = feishu.get_record(record_id)
    owners = fields.get(mapping["assignee"]) or []
    if not owners or fields.get(mapping["cancelled"]) in (True, 1) or fields.get(mapping["completed_at"]):
        return "skipped"
    if event == "deadline_reminder":
        due = deadline_date(fields.get(mapping["deadline"]))
        today = datetime.now(ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))).date()
        if due != today:
            logger.debug("截止提醒跳过：截止日期=%s 当前日期=%s", due, today)
            return "skipped"
        allowed_fields = {"assignee"}
        # 同一截止日每位负责人仅提醒一次，改期后使用新的通知版本。
        version = due.isoformat()
    else:
        if fields.get(mapping[event]) not in (True, 1):
            return "skipped"
        allowed_fields = {"assignee", "creator"}
        # 负责人发生变化时按新的分配通知；重复触发同一分配不会重复发送。
        version = json.dumps([p["id"] for p in owners])
    source_key = config.get("recipient_field")
    if source_key not in allowed_fields:
        raise ValueError(f"{event}.recipient_field 需为 {' 或 '.join(sorted(allowed_fields))}")
    source = fields.get(mapping[source_key]) or []
    if isinstance(source, dict):
        source = [source]
    if not source:
        raise ValueError("通知接收人为空")
    bot = FeishuClient(replace(settings, app_id=settings.alert_app_id, app_secret=settings.alert_app_secret))
    recipients = []
    for person in source:
        uid = alert_open_id(person["id"], feishu, bot)
        if uid not in {p["open_id"] for p in recipients}:
            recipients.append({"open_id": uid, "name": person.get("name") or "记录人员"})
    if not deliver(record_id, mapping, feishu, recipients, event, fields, version):
        raise RuntimeError("部分通知发送失败，可重试同一请求")
    return "sent_or_duplicate"


def deliver(record_id, mapping, feishu, recipients, event, fields=None, version=""):
    """功能说明：按接收人持久化去重并发送卡片。参数 record_id：记录 ID；mapping：字段映射；feishu：数据客户端；recipients：接收人；event：事件；fields：可选记录快照；version：通知版本。返回值：是否全部成功。"""
    settings = feishu.settings
    if not settings.alert_app_id or not settings.alert_app_secret:
        raise ValueError("请配置 FEISHU_ALERT_APP_ID 和 FEISHU_ALERT_APP_SECRET")
    bot = FeishuClient(replace(settings, app_id=settings.alert_app_id, app_secret=settings.alert_app_secret))
    folder = settings.rules_cache_path.parent / "notifications"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    success = True
    content = None
    for person in recipients:
        # 按应用、表、记录和接收人去重；锁覆盖发送与落盘，供后台和 CLI 共用。
        identity = [settings.alert_app_id, settings.base_token, settings.table_id, record_id, person["open_id"], event]
        if version:
            identity.append(version)
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        path = folder / f"{key}.json"
        try:
            with (folder / f"{key}.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                state = json.loads(path.read_text()) if path.exists() else {}
                if state.get("sent"):
                    logger.debug("通知跳过：已发送 event=%s recipient=%s", event, person["name"])
                    continue
                # 飞书 UUID 仅保证一小时去重；超时的未知结果不能自动重发。
                if state and time.time() - state["started_at"] >= 3600:
                    raise ValueError("通知发送结果未知且已超过一小时，请核对聊天记录与通知缓存")
                if not state:
                    if content is None:
                        snapshot = fields if fields is not None else feishu.get_record(record_id)
                        content = notification_content(record_id, snapshot, mapping, settings, feishu, event)
                    state = {"sent": False, "started_at": time.time(), "content": content}
                    save_state(path, state)
                message_id = bot.send_card(person["open_id"], state["content"], key[:50])
                save_state(path, {"sent": True, "message_id": message_id})
                logger.info("通知已发送 事件=%s 接收人=%s", event, person["name"])
        except Exception as exc:
            success = False
            logger.warning("通知失败 事件=%s 接收人=%s 原因=%s", event, person["name"], str(exc))
    return success


def save_state(path, state):
    """原子落盘通知状态；同一通知的调用方已持有文件锁。"""
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(state, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
