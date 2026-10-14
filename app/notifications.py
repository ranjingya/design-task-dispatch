"""新增需求通知：读取容器外配置，使用独立应用机器人私聊并持久化去重。"""

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
from urllib.parse import quote

from app.feishu import FeishuClient
from app.rules import plain

logger = logging.getLogger(__name__)


def load_notifications(path):
    """功能说明：读取通知配置。参数 path：TOML 路径。返回值：通过校验的新增需求配置，未启用时返回空字典。"""
    if not path.exists():
        logger.debug("新增通知未配置")
        return {}
    config = tomllib.loads(path.read_text(encoding="utf-8")).get("new_task", {})
    if not isinstance(config, dict) or type(config.get("enabled", False)) is not bool:
        raise ValueError("new_task.enabled 必须是布尔值")
    if not config.get("enabled", False):
        return {}
    if not isinstance(config.get("source_name", ""), str):
        raise ValueError("new_task.source_name 必须为文本")
    people = config.get("recipients", [])
    if not isinstance(people, list):
        raise ValueError("new_task.recipients 必须为人员列表")
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


def notification_content(record_id, fields, mapping, settings, feishu, source_name):
    """功能说明：按上新样式生成紫色卡片。参数 record_id：记录 ID；fields：回写后字段；mapping：字段映射；settings：数据配置；feishu：人员身份查询客户端；source_name：来源表显示名称。返回值：飞书 Card 2.0 内容。"""
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
                    if settings.app_id == settings.alert_app_id:
                        resolved[source_id] = source_id
                    else:
                        union_id = feishu.get_contact_user(source_id).get("union_id")
                        if not isinstance(union_id, str) or not union_id.startswith("on_"):
                            raise ValueError("人员 Union ID 不可用")
                        resolved[source_id] = bot.get_contact_user_by_union_id(union_id).get("open_id", "")
                uid = resolved[source_id]
                if not isinstance(uid, str) or not re.fullmatch(r"ou_[A-Za-z0-9]{1,125}", uid):
                    raise ValueError("通知应用 Open ID 不可用")
                result.append(f'<at id="{uid}"></at>')
            except Exception:
                logger.warning("通知人员引用不可用，使用姓名展示")
                result.append(escape_text(person["name"]))
        return " ".join(result)

    if not value("rule_explanation") or not value("recommendation_result"):
        raise ValueError("匹配与推荐结果尚未就绪，暂不发送通知")
    if str(value("rule_explanation")).startswith("处理失败"):
        raise ValueError("任务处理失败，暂不发送新增通知")
    lines = [
        ("任务名称", escape_text(value("title")) or "未命名需求"),
        ("需求说明", escape_text(value("description")) or "未填写"),
        ("需求人员", people("submitter") or "未填写"),
        ("推荐负责人", people("recommended_assignee") or "待人工分配"),
        ("所属平台", escape_text(value("platform")) or "未填写"),
    ]
    base = quote(settings.base_token, safe="")
    table = quote(settings.table_id, safe="")
    record = quote(record_id, safe="")
    source_url = f"https://feishu.cn/base/{base}?table={table}"
    elements = [
        {"tag": "markdown", "content": "\n".join(f"**{label}：** {text}" for label, text in lines)},
        {"tag": "button", "text": {"tag": "plain_text", "content": "查看需求"}, "type": "default",
         "behaviors": [{"type": "open_url", "default_url": source_url + f"&record={record}"}]},
        {"tag": "hr"},
        {"tag": "markdown", "content": f"来自 [{escape_text(source_name)}]({source_url})"},
    ]
    return {"schema": "2.0", "config": {"width_mode": "default"},
            "header": {"title": {"tag": "plain_text", "content": "美工任务上新"}, "template": "purple"},
            "body": {"direction": "vertical", "vertical_spacing": "12px", "elements": elements}}


def send_new_task(record_id, mapping, feishu):
    """功能说明：读取最新结果，逐人发送一次新增通知；失败可单独补发。参数 record_id：需求记录 ID；mapping：字段映射；feishu：数据应用客户端。返回值：所有启用接收人发送成功或已发送时为真，存在失败时为假。"""
    settings = feishu.settings
    config = load_notifications(settings.fields_path.parent / "notifications.toml")
    recipients = [p for p in config.get("recipients", []) if p["enabled"]]
    if not recipients:
        logger.debug("新增通知跳过：未启用接收人")
        return True
    if not settings.alert_app_id or not settings.alert_app_secret:
        raise ValueError("请配置 FEISHU_ALERT_APP_ID 和 FEISHU_ALERT_APP_SECRET")
    bot = FeishuClient(replace(settings, app_id=settings.alert_app_id, app_secret=settings.alert_app_secret))
    folder = settings.rules_cache_path.parent / "notifications"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    success = True
    content = None
    for person in recipients:
        # 按应用、表、记录和接收人去重；锁覆盖发送与落盘，供后台和 CLI 共用。
        identity = [settings.alert_app_id, settings.base_token, settings.table_id, record_id, person["open_id"], "new_task"]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        path = folder / f"{key}.json"
        try:
            with (folder / f"{key}.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                state = json.loads(path.read_text()) if path.exists() else {}
                if state.get("sent"):
                    logger.debug("新增通知跳过：已发送 recipient=%s", person["name"])
                    continue
                # 飞书 UUID 仅保证一小时去重；超时的未知结果不能自动重发。
                if state and time.time() - state["started_at"] >= 3600:
                    raise ValueError("通知发送结果未知且已超过一小时，请核对聊天记录与通知缓存")
                if not state:
                    if content is None:
                        fields = feishu.get_record(record_id)
                        content = notification_content(record_id, fields, mapping, settings, feishu, config.get("source_name", "需求任务表"))
                    state = {"sent": False, "started_at": time.time(), "content": content}
                    save_state(path, state)
                message_id = bot.send_card(person["open_id"], state["content"], key[:50])
                save_state(path, {"sent": True, "message_id": message_id})
                logger.info("新增通知已发送 接收人=%s", person["name"])
        except Exception as exc:
            success = False
            logger.warning("新增通知失败 接收人=%s 原因=%s", person["name"], str(exc))
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
