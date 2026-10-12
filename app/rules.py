"""规则标准化、部门筛选与可读结果生成。"""

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def plain(value) -> str:
    """将飞书文本、单选或富文本转为可读字符串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(plain(item) for item in value)
    if isinstance(value, dict):
        return plain(value.get("text", ""))
    return ""


def normalize_rules(records: list[dict], path: Path) -> list[dict]:
    """功能说明：按外部映射标准化规则。参数 records：飞书原始规则；path：规则字段映射路径。返回值：完整规则快照。"""
    logger.info("加载规则字段配置：%s", path)
    mapping = json.loads(path.read_text(encoding="utf-8"))
    required = {"work_type", "content", "departments", "default_level", "adjustment", "assignment"}
    if not isinstance(mapping, dict) or not required <= mapping.keys() or any(
        not isinstance(mapping[k], str) or not mapping[k].strip() for k in required
    ):
        raise ValueError("规则字段映射不完整")
    rules = []
    for record in records:
        rule = {key: plain(record["fields"].get(mapping[key])) for key in required}
        rule["record_id"] = record["record_id"]
        rule["departments"] = [x.strip() for x in re.split(r"[、,，;/；\n]+", rule["departments"]) if x.strip()]
        match = re.match(r"^([ABC])", rule["default_level"])
        if not match or not rule["work_type"] or not rule["content"] or not rule["departments"]:
            raise ValueError(f"规则缺少类型、内容、部门或等级：{rule['record_id']}")
        rule["default_level"] = match[1]
        rules.append(rule)
    if not rules:
        raise ValueError("没有可用规则")
    return rules


def select_department(platform, rules: list[dict]) -> tuple[str | None, list[dict]]:
    """功能说明：按平台中明确的部门筛选规则。参数 platform：提报平台；rules：完整规则。返回值：确认部门及候选范围。"""
    platform = plain(platform)
    departments = {d for rule in rules for d in rule["departments"]}
    matches = {d for d in departments if d in platform}
    if len(matches) == 1:
        department = matches.pop()
        eligible = [r for r in rules if department in r["departments"]]
        logger.info("部门筛选完成：department=%s rule_count=%d", department, len(eligible))
        return department, eligible
    logger.warning("提报平台无法唯一确定部门：matched_count=%d", len(matches))
    return None, rules


def review(reason: str, missing: list[str], department=None) -> dict:
    """创建无法准确匹配的结构化结果。"""
    return {"matched": False, "department": department, "rule_id": None,
            "work_type": None, "level": None, "reason": None, "candidates": [],
            "unmatched_reason": reason, "missing_information": missing}


def format_result(result: dict, rules: list[dict]) -> str:
    """功能说明：生成标注具体规则的回写文本。参数 result：校验后结果；rules：本次规则快照。返回值：可读匹配说明。"""
    by_id = {r["record_id"]: r for r in rules}

    def describe(rule):
        return (f"规则：{rule['work_type']}（{rule['record_id']}）\n"
                f"规则内容：{rule['content']}\n适用部门：{'、'.join(rule['departments'])}\n"
                f"分配方式：{rule['assignment']}")

    if result["matched"]:
        return ("准确匹配\n" + describe(by_id[result["rule_id"]]) +
                f"\n评级：{result['level']}\n判断依据：{result['reason']}")
    lines = ["无法准确匹配", "未匹配原因：" + result["unmatched_reason"]]
    for index, candidate in enumerate(result["candidates"], 1):
        lines.extend([f"\n候选 {index}：" + describe(by_id[candidate["rule_id"]]),
                      "可能匹配：" + candidate["match_reason"], "未确定原因：" + candidate["uncertainty"]])
    if not result["candidates"]:
        lines.append("候选规则：暂无可确认的候选")
    if result["missing_information"]:
        lines.append("需补充：" + "；".join(result["missing_information"]))
    return "\n".join(lines)
