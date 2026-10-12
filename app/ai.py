"""DeepSeek 多模态评级与结构化输出校验。"""

import json
import logging
import re
import time

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


def validate_result(result: dict, options: list[str], rules: list[dict]) -> dict:
    """功能说明：校验模型输出。参数 result：模型 JSON；options：允许的工作类型；rules：本次规则快照。返回值：通过校验的结果。"""
    if not isinstance(result, dict) or result.get("status") not in ("rated", "needs_review"):
        raise ValueError("AI 输出状态无效")
    reason = result.get("reason")
    missing = result.get("missing_information")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ValueError("AI 判断依据为空或过长")
    if not isinstance(missing, list) or len(missing) > 20 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 500 for item in missing
    ):
        raise ValueError("AI 待确认事项格式错误")
    rule_id = result.get("matched_rule_id")
    if rule_id is not None and (not isinstance(rule_id, str) or rule_id not in {r["record_id"] for r in rules}):
        raise ValueError("AI 引用了不存在的规则")
    if result["status"] == "rated":
        work_type = result.get("work_type")
        if not isinstance(work_type, str) or work_type not in options:
            raise ValueError("AI 返回了未配置的工作类型")
        match = re.match(r"^([ABC])\s*[-－—]", work_type)
        if not match or result.get("level") != match[1] or rule_id is None or missing:
            raise ValueError("AI 评级、工作类型或规则信息不一致")
    elif result.get("work_type") is not None or result.get("level") is not None:
        raise ValueError("待确认结果不能同时包含确定评级")
    return result


class AIClient:
    def __init__(self, settings: Settings):
        """初始化模型网关；settings 为接口、模型及超时配置。"""
        self.settings = settings

    def classify(self, demand: dict, rules: list[dict], options: list[str], images: list[dict]) -> dict:
        """功能说明：调用模型并校验评级。参数 demand：需求文本字段；rules：规则快照；options：允许选项；images：图片消息片段。返回值：校验后的评级结果。"""
        settings = self.settings
        settings.validate_ai()
        logger.info("加载评级提示词：%s", settings.prompt_path)
        prompt = settings.prompt_path.read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError("评级提示词为空")
        content = [{"type": "text", "text": json.dumps({
            "demand": demand, "rules": rules, "allowed_work_types": options,
        }, ensure_ascii=False)}, *images]
        payload = {
            "model": settings.ai_model,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "max_tokens": 4096,
            "thinking": {"type": "disabled"},
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
                    result = validate_result(result, options, rules)
                    logger.info("AI 评级完成：status=%s", result["status"])
                    return result
                failure = f"AI 请求失败：HTTP {response.status_code}"
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(failure)
            logger.warning("%s，尝试 %d/%d", failure, attempt, settings.ai_attempts)
            if attempt < settings.ai_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
        raise RuntimeError(failure)
