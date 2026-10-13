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

    def classify(self, demand: dict, rules: list[dict], options: list[str], images: list[dict], department: str | None = None, *, material=None) -> dict:
        """功能说明：通过总览和按需补图完成评级。参数 demand：需求文字；rules：规则快照；options：允许类型；images：单图预览；department：已确认部门；material：可选材料快照及原图切片。返回值：校验后的最终评级。"""
        from app.visual_review import VisualReview, visual_config
        from app.rules import review

        settings = self.settings
        settings.validate_ai()
        started = time.monotonic()
        logger.info("加载评级提示词：%s", settings.prompt_path)
        prompt = settings.prompt_path.read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError("评级提示词为空")
        config = material.config if material is not None else json.loads(settings.materials_path.read_text())
        visual = VisualReview(images, demand.get("image_sources", []), visual_config(config),
                              material.image_parts if material is not None else None, demand)
        keys, batch = visual.take(first=True)
        context = {"demand": {key: value for key, value in demand.items() if key != "image_sources"}}
        if images:
            context["visual_review"] = visual.state(1, keys)
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": [{"type": "text", "text": json.dumps(context, ensure_ascii=False)}, *batch]}]
        rounds = visual.config["max_rounds"] if images else 1
        last_review = None
        selection_prompt = None
        for number in range(1, rounds + 1):
            selecting = bool(visual.pending())
            if selecting:
                if selection_prompt is None:
                    selection_path = settings.prompt_path.with_name("select_images.md")
                    logger.info("加载选图提示词：%s", selection_path)
                    selection_prompt = selection_path.read_text(encoding="utf-8")
                    if not selection_prompt.strip():
                        raise ValueError("选图提示词为空")
                messages[0]["content"] = selection_prompt
            else:
                messages[0]["content"] = prompt
                content = json.loads(messages[-1]["content"][0]["text"])
                content.update({"rules": rules, "allowed_work_types": options, "confirmed_department": department})
                messages[-1]["content"][0]["text"] = json.dumps(content, ensure_ascii=False)
            logger.info("AI 本轮阶段：round=%d phase=%s", number, "选图" if selecting else "评级")
            logger.info("图片核对：round=%d new_images=%d sent_images=%d original_images=%d", number, len(batch), len(visual.sent), len(images))
            logger.info("本轮图片编号：round=%d keys=%s", number, keys)
            result, assistant = self._complete(messages)
            if not isinstance(result, dict):
                raise ValueError("AI 输出不是 JSON 对象")
            requested = result.get("action") == "request_images"
            if requested:
                visual.request(result.get("requests"))
            else:
                if result.get("action") not in (None, "final"):
                    raise ValueError("AI 返回了未知操作")
                result = validate_result(result, options, rules, department)
                if not result["matched"]:
                    last_review = result
                if demand.get("material_issues") and result["matched"]:
                    raise ValueError("材料未完整读取时不得给出确定评级")
                if not visual.pending():
                    logger.info("AI 评级完成：matched=%s rounds=%d sent_images=%d elapsed=%.2fs", result["matched"], number, len(visual.sent), time.monotonic() - started)
                    return result
            pending = visual.pending()
            if number == rounds or not pending:
                required = [row["source"] for row in visual.catalog if any(key in pending for key in visual.original_keys[row["image_index"]])]
                missing = ["请核对说明图片：" + name[:200] for name in required[:10]]
                if any(key.startswith("overview:") for key in pending):
                    missing.append("仍有图片总览未检查，请拆分材料或补充文字要求。")
                if not missing:
                    missing = ["已提供的图片仍不足以判断，请补充明确的工作要求。"]
                logger.warning("图片核对待确认：rounds=%d pending=%d elapsed=%.2fs", number, len(pending), time.monotonic() - started)
                fallback = review("图片核对已达轮次上限，仍缺少判断依据。" if pending else "现有图片无法确认需求。", missing, department)
                if last_review:
                    fallback["candidates"] = last_review["candidates"]
                    fallback["unmatched_reason"] = last_review["unmatched_reason"] + " " + fallback["unmatched_reason"]
                return fallback
            messages.append(assistant)
            keys, batch = visual.take()
            messages.append({"role": "user", "content": [{"type": "text", "text": json.dumps({
                "visual_review": visual.state(number + 1, keys),
                "instruction": "请核对本轮图片；未读总览与必读原图尚未覆盖时不能结束。信息足够则返回最终评级，否则点选需要的图片。",
            }, ensure_ascii=False)}, *batch]})
        raise RuntimeError("图片核对流程未正常结束")

    def _complete(self, messages):
        """功能说明：调用模型并解析一次完整响应。参数 messages：包含历史图片的消息列表。返回值：JSON 对象与可延续对话的助手消息。"""
        settings = self.settings
        started = time.monotonic()
        payload = {"model": settings.ai_model, "messages": messages,
                   "response_format": {"type": "json_object"}, "max_tokens": settings.ai_max_tokens,
                   "thinking": {"type": "enabled"}}
        # Base64 会放大附件体积，在发送前检查完整请求，避免接口拒收。
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 45 * 1024 * 1024:
            raise ValueError("模型请求体超过大小限制")
        for attempt in range(1, settings.ai_attempts + 1):
            logger.info("调用 AI：model=%s attempt=%d image_count=%d", settings.ai_model, attempt, sum(item.get("type") == "image_url" for msg in messages if isinstance(msg.get("content"), list) for item in msg["content"]))
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
                            logger.warning("AI 输出未完整结束：finish_reason=%s max_tokens=%d", choice.get("finish_reason"), settings.ai_max_tokens)
                            raise ValueError("AI 输出未完整结束")
                        result = json.loads(choice["message"]["content"])
                    except (KeyError, IndexError, TypeError, ValueError):
                        raise ValueError("AI 响应为空、被截断或不是有效 JSON") from None
                    logger.info("AI 响应完成：elapsed=%.2fs", time.monotonic() - started)
                    return result, {"role": "assistant", "content": choice["message"]["content"]}
                failure = f"AI 请求失败：HTTP {response.status_code}"
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(failure)
            logger.warning("%s，尝试 %d/%d", failure, attempt, settings.ai_attempts)
            if attempt < settings.ai_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
        raise RuntimeError(failure)
