"""DeepSeek 多模态评级与结构化输出校验。"""

import json
import logging
import time

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


def validate_result(result: dict, options: list[str], rules: list[dict], department: str | None = None) -> dict:
    """功能说明：校验模型引用、部门和评级。参数 result：模型结果；options：类型选项；rules：标准化规则；department：已确认部门。返回值：有效结果。"""
    if not isinstance(result, dict) or type(result.get("matched")) is not bool:
        raise ValueError("AI 匹配状态无效")
    by_id = {r["record_id"]: r for r in rules}

    def text(value, limit=4000):
        return isinstance(value, str) and bool(value.strip()) and len(value) <= limit

    def valid_rule(rule_id):
        if not isinstance(rule_id, str) or rule_id not in by_id:
            raise ValueError("AI 引用了不存在的规则")
        rule = by_id[rule_id]
        if department and department not in rule["departments"]:
            raise ValueError("AI 引用了其他部门的规则")
        return rule

    missing = result.get("missing_information")
    candidates = result.get("candidates")
    if not isinstance(missing, list) or len(missing) > 20 or any(not text(x, 500) for x in missing):
        raise ValueError("待补充信息格式无效")
    if not isinstance(candidates, list) or len(candidates) > 3:
        raise ValueError("候选规则格式无效")
    if result.get("department") != department:
        raise ValueError("AI 不得改变已确认的部门")
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("候选规则格式无效")
        rule = valid_rule(candidate.get("rule_id"))
        if rule["record_id"] in seen or not text(candidate.get("match_reason")) or not text(candidate.get("uncertainty")):
            raise ValueError("候选重复或缺少判断依据")
        seen.add(rule["record_id"])
    if result["matched"]:
        rule = valid_rule(result.get("rule_id"))
        if not department or result.get("work_type") != rule["work_type"] or rule["work_type"] not in options:
            raise ValueError("部门或工作类型与规则不一致")
        if result.get("level") not in ("A", "B", "C") or not text(result.get("reason")):
            raise ValueError("等级或评级依据无效")
        if not rule["adjustment"] and result["level"] != rule["default_level"]:
            raise ValueError("没有调整说明的规则必须使用默认等级")
        if missing or candidates or result.get("unmatched_reason") is not None:
            raise ValueError("准确匹配不能包含待确认事项")
    elif any(result.get(k) is not None for k in ("rule_id", "work_type", "level")) or not text(result.get("unmatched_reason")):
        raise ValueError("未匹配结果不得包含确定评级，且必须说明原因")
    return result


class AIClient:
    def __init__(self, settings: Settings):
        """初始化模型网关；settings 为接口、模型及超时配置。"""
        self.settings = settings

    def classify(self, demand: dict, rules: list[dict], options: list[str], images: list[dict], department: str | None = None) -> dict:
        """功能说明：调用模型并校验评级。参数 demand：需求文本字段；rules：规则快照；options：允许选项；images：图片消息片段；department：从提报平台确认的部门。返回值：校验后的评级结果。"""
        settings = self.settings
        settings.validate_ai()
        logger.info("加载评级提示词：%s", settings.prompt_path)
        prompt = settings.prompt_path.read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError("评级提示词为空")
        content = [{"type": "text", "text": json.dumps({
            "demand": demand, "rules": rules, "allowed_work_types": options,
            "confirmed_department": department,
        }, ensure_ascii=False)}, *images]
        payload = {
            "model": settings.ai_model,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "max_tokens": 8192,
            "thinking": {"type": "enabled"},
        }
        # Base64 会放大附件体积，在发送前检查完整请求，避免接口拒收。
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 45 * 1024 * 1024:
            raise ValueError("模型请求体超过大小限制")
        for attempt in range(1, settings.ai_attempts + 1):
            logger.info("调用 AI：model=%s attempt=%d image_count=%d", settings.ai_model, attempt, len(images))
            try:
                response = httpx.post(settings.ai_base_url + "/chat/completions", json=payload,
                                      headers={"Authorization": f"Bearer {settings.ai_key}"},
                                      timeout=settings.ai_timeout)
            except httpx.RequestError:
                failure = "AI 网络异常或超时"
            else:
                if response.is_success:
                    try:
                        choice = response.json()["choices"][0]
                        if choice.get("finish_reason") != "stop":
                            raise ValueError("AI 输出未完整结束")
                        result = json.loads(choice["message"]["content"])
                    except (KeyError, IndexError, TypeError, ValueError):
                        raise ValueError("AI 响应为空、被截断或不是有效 JSON") from None
                    result = validate_result(result, options, rules, department)
                    if demand.get("material_issues") and result["matched"]:
                        raise ValueError("材料未完整读取时不得给出确定评级")
                    logger.info("AI 评级完成：matched=%s", result["matched"])
                    return result
                failure = f"AI 请求失败：HTTP {response.status_code}"
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(failure)
            logger.warning("%s，尝试 %d/%d", failure, attempt, settings.ai_attempts)
            if attempt < settings.ai_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
        raise RuntimeError(failure)
