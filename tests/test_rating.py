"""评级链路、外部接口及异常回写测试。"""

import json
from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest

from app.ai import AIClient, validate_result
from app.config import ROOT, Settings
from app.dispatch import process_record
from app.feishu import FeishuClient
from app.materials import collect_images
from app.rules import normalize_rules, select_department, format_result

MAPPING = {"title": "标题", "description": "说明", "attachments": "材料", "platform": "平台",
           "work_type": "类别", "level": "等级", "rule_explanation": "结果"}
RULES = [{"record_id": "recRule", "work_type": "测试类型", "content": "测试工作", "departments": ["测试部"],
          "default_level": "A", "adjustment": "", "assignment": "轮流"}]
RAW_RULES = [{"record_id": "recRule", "fields": {"工作类型": "测试类型", "包含的工作": "测试工作",
              "适用部门": "测试部", "默认ABC分类": "A基础重复", "分类调整说明": "", "匹配方式": "轮流"}}]
OPTIONS = ["测试类型"]
SCHEMA = [{"field_name": "类别", "type": 3, "property": {"options": [{"name": x} for x in OPTIONS]}},
          {"field_name": "等级", "type": 3, "property": {"options": [{"name": x} for x in "ABC"]}},
          {"field_name": "结果", "type": 1}, {"field_name": "材料", "type": 17, "field_id": "fldImage"}]
RATED = {"matched": True, "department": "测试部", "level": "A", "work_type": "测试类型", "rule_id": "recRule",
         "reason": "符合规则", "missing_information": [], "candidates": [], "unmatched_reason": None}
REVIEW = {"matched": False, "department": "测试部", "level": None, "work_type": None, "rule_id": None,
          "reason": None, "missing_information": ["需补充用途"], "unmatched_reason": "用途不清晰",
          "candidates": [{"rule_id": "recRule", "match_reason": "内容相符", "uncertainty": "用途未确定"}]}


@pytest.fixture
def settings(tmp_path):
    return Settings("app", "secret", "base", "table", "token", tmp_path / "fields.json",
                    rules_table_id="rules", ai_key="private-key", ai_model="test-model",
                    prompt_path=ROOT / "prompts/classify.md")


@pytest.fixture
def feishu(settings):
    client = Mock(spec=FeishuClient)
    client.settings = settings
    client.get_fields.return_value = SCHEMA
    client.get_rules.return_value = RAW_RULES
    client.get_record.return_value = {"标题": "测试", "说明": "测试需求", "平台": "测试部-平台"}
    return client


def test_success_writes_three_fields(feishu, monkeypatch):
    classify = Mock(return_value=RATED)
    monkeypatch.setattr(AIClient, "classify", classify)
    feishu.get_record.return_value.update({"类别": "旧类别", "结果": "旧原因", "最终负责人": "某人"})
    process_record("recTest", MAPPING, feishu)
    fields = feishu.update_record.call_args.args[1]
    assert set(fields) == {"类别", "等级", "结果"}
    assert fields["类别"] == "测试类型"
    assert "recRule" in fields["结果"]
    assert classify.call_args.args[0]["platform"] == "测试部-平台"
    assert fields["等级"] == "A"


def test_missing_input_writes_review_without_ai(feishu, monkeypatch):
    classify = Mock()
    monkeypatch.setattr(AIClient, "classify", classify)
    feishu.get_record.return_value = {"标题": "测试"}
    process_record("recTest", MAPPING, feishu)
    classify.assert_not_called()
    assert feishu.update_record.call_args.args[1]["类别"] is None
    assert "说明" in feishu.update_record.call_args.args[1]["结果"]


def test_unsupported_attachment_provides_candidates(feishu, monkeypatch):
    classify = Mock(return_value=REVIEW)
    monkeypatch.setattr(AIClient, "classify", classify)
    feishu.get_record.return_value["材料"] = [{"file_token": "file", "type": "application/pdf"}]
    process_record("recTest", MAPPING, feishu)
    assert "附件 1" in classify.call_args.args[0]["material_issues"][0]
    assert "候选 1" in feishu.update_record.call_args.args[1]["结果"]


def test_model_failure_is_retryable_and_does_not_write_type(feishu, monkeypatch):
    monkeypatch.setattr(AIClient, "classify", Mock(side_effect=RuntimeError("模拟失败")))
    with pytest.raises(RuntimeError):
        process_record("recTest", MAPPING, feishu)
    assert set(feishu.update_record.call_args.args[1]) == {"结果"}
    assert "处理失败" in feishu.update_record.call_args.args[1]["结果"]


def test_invalid_schema_never_writes(feishu):
    feishu.get_fields.return_value = []
    with pytest.raises(ValueError):
        process_record("recTest", MAPPING, feishu)
    feishu.update_record.assert_not_called()


@pytest.mark.parametrize("change", [
    {"work_type": "C-不存在"}, {"rule_id": "recFake"}, {"level": "B"},
    {"missing_information": ["缺信息"]}, {"reason": ""}, {"matched": False},
    {"work_type": []}, {"rule_id": []},
])
def test_invalid_model_result_rejected(change):
    with pytest.raises(ValueError):
        validate_result(RATED | change, OPTIONS, RULES, "测试部")


def test_review_output_accepted():
    assert validate_result(REVIEW, OPTIONS, RULES, "测试部") == REVIEW


def test_ai_retries_transient_failure_and_sends_json_images(settings, monkeypatch):
    response = httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(RATED)}}]})
    post = Mock(side_effect=[httpx.Response(429), response])
    monkeypatch.setattr("app.ai.httpx.post", post)
    monkeypatch.setattr("app.ai.time.sleep", Mock())
    images = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]
    result = AIClient(settings).classify({"title": "测试"}, RULES, OPTIONS, images, "测试部")
    assert result == RATED and post.call_count == 2
    payload = post.call_args.kwargs["json"]
    assert payload["messages"][1]["content"][1:] == images
    assert payload["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("response", [
    httpx.Response(401),
    httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}),
    httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}),
])
def test_ai_permanent_or_bad_output_does_not_retry(settings, monkeypatch, response):
    post = Mock(return_value=response)
    monkeypatch.setattr("app.ai.httpx.post", post)
    with pytest.raises((RuntimeError, ValueError)):
        AIClient(settings).classify({}, RULES, OPTIONS, [])
    assert post.call_count == 1


def test_timeout_has_bounded_retry_and_no_secret(settings, monkeypatch):
    post = Mock(side_effect=httpx.ReadTimeout("私密请求正文"))
    monkeypatch.setattr("app.ai.httpx.post", post)
    monkeypatch.setattr("app.ai.time.sleep", Mock())
    with pytest.raises(RuntimeError, match="AI 网络异常或超时") as exc:
        AIClient(settings).classify({}, RULES, OPTIONS, [])
    assert post.call_count == 3
    assert "私密" not in str(exc.value)


def test_pagination_and_repeated_cursor(settings, monkeypatch):
    client = FeishuClient(settings)
    monkeypatch.setattr(client, "_access_token", lambda: "token")
    request = Mock(side_effect=[{"data": {"items": RAW_RULES, "has_more": True, "page_token": "next"}},
                               {"data": {"items": [{"record_id": "recTwo", "fields": {"x": "y"}}]}}])
    monkeypatch.setattr(client, "_request", request)
    assert len(client.get_rules()) == 2
    request.side_effect = None
    request.return_value = {"data": {"items": [], "has_more": True, "page_token": "same"}}
    with pytest.raises(RuntimeError, match="分页"):
        client.get_rules()


def test_image_limits_content_and_download_errors(feishu):
    feishu.settings = replace(feishu.settings, max_images=1)
    feishu.download_image.return_value = b"\x89PNG\r\n\x1a\ncontent"
    item = {"file_token": "file", "type": "image/png", "size": 20}
    images, issues = collect_images([item, item], "recTest", "fldImage", feishu)
    assert len(images) == 1 and len(issues) == 1
    assert "data:image/png;base64," in images[0]["image_url"]["url"]
    feishu.download_image.return_value = b"not an image"
    assert collect_images([item], "recTest", "fldImage", feishu)[1]
    feishu.download_image.side_effect = RuntimeError("下载失败")
    assert collect_images([item], "recTest", "fldImage", feishu)[1]


def test_download_stream_limit_and_permission_context(settings, monkeypatch):
    client = FeishuClient(settings)
    monkeypatch.setattr(client, "_access_token", lambda: "token")
    response = Mock(is_success=True)
    response.iter_bytes.return_value = iter([b"123", b"456"])
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    stream = Mock(return_value=context)
    monkeypatch.setattr("app.feishu.httpx.stream", stream)
    with pytest.raises(ValueError, match="大小"):
        client.download_image("file", "recTest", "fldImage", 5)
    extra = json.loads(stream.call_args.kwargs["params"]["extra"])
    assert extra["bitablePerm"]["attachments"] == {"fldImage": {"recTest": ["file"]}}


def test_needs_review_clears_stale_rating(feishu, monkeypatch):
    monkeypatch.setattr(AIClient, "classify", Mock(return_value=REVIEW))
    process_record("recTest", MAPPING, feishu)
    updates = feishu.update_record.call_args.args[1]
    assert updates["类别"] is None and updates["等级"] is None
    assert "测试工作" in updates["结果"] and "用途未确定" in updates["结果"]


def test_failed_write_does_not_report_success(feishu, monkeypatch):
    monkeypatch.setattr(AIClient, "classify", Mock(return_value=RATED))
    feishu.update_record.side_effect = RuntimeError("模拟写入失败")
    with pytest.raises(RuntimeError, match="模拟写入失败"):
        process_record("recTest", MAPPING, feishu)


def test_secret_not_in_settings_repr(settings):
    assert 'private-key' not in repr(settings)
    assert "app_secret=" not in repr(settings)


@pytest.mark.parametrize("updates", [{"ai_attempts": 0}, {"ai_timeout": -1}, {"ai_base_url": "http://unsafe"},
                                      {"ai_key": ""}, {"rules_table_id": ""}, {"max_images": 0}])
def test_invalid_runtime_config_rejected(settings, updates):
    with pytest.raises(ValueError):
        replace(settings, **updates).validate_ai()


def test_department_filter_and_cross_department_rejection():
    other = RULES[0] | {"record_id": "recOther", "departments": ["其他部"]}
    assert select_department("测试部-平台", RULES + [other]) == ("测试部", RULES)
    assert select_department("未知平台", RULES)[0] is None
    with pytest.raises(ValueError, match="其他部门"):
        validate_result(RATED | {"rule_id": "recOther"}, OPTIONS, RULES + [other], "测试部")
    with pytest.raises(ValueError):
        validate_result(RATED | {"department": None}, OPTIONS, RULES, None)
    with pytest.raises(ValueError, match="其他部门"):
        validate_result(REVIEW | {"candidates": [REVIEW["candidates"][0] | {"rule_id": "recOther"}]},
                        OPTIONS, RULES + [other], "测试部")


def test_rules_normalization_and_duplicate_names():
    rules = normalize_rules(RAW_RULES, ROOT / "config/rule_fields.json")
    assert rules == RULES
    second = RULES[0] | {"record_id": "recSecond", "content": "另一工作", "assignment": "固定人员"}
    output = format_result(RATED | {"rule_id": "recSecond"}, RULES + [second])
    assert "另一工作" in output and "固定人员" in output and "recSecond" in output


def test_material_failure_cannot_produce_rating(settings, monkeypatch):
    response = httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(RATED)}}]})
    monkeypatch.setattr("app.ai.httpx.post", Mock(return_value=response))
    with pytest.raises(ValueError, match="材料"):
        AIClient(settings).classify({"material_issues": ["无法读取"]}, RULES, OPTIONS, [], "测试部")


def test_prompt_is_loaded_for_each_call(settings, monkeypatch, tmp_path):
    prompt = tmp_path / 'classify.md'
    settings = replace(settings, prompt_path=prompt)
    response = httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(RATED)}}]})
    post = Mock(return_value=response)
    monkeypatch.setattr('app.ai.httpx.post', post)
    client = AIClient(settings)
    for content in ('第一份 JSON 提示词', '第二份 JSON 提示词'):
        prompt.write_text(content)
        client.classify({}, RULES, OPTIONS, [], '测试部')
        assert post.call_args.kwargs['json']['messages'][0]['content'] == content


def test_pipeline_only_sends_department_rules(feishu, monkeypatch):
    other = {"record_id": "recOther", "fields": RAW_RULES[0]["fields"] | {"适用部门": "其他部"}}
    feishu.get_rules.return_value = RAW_RULES + [other]
    classify = Mock(return_value=RATED)
    monkeypatch.setattr(AIClient, 'classify', classify)
    process_record('recTest', MAPPING, feishu)
    assert classify.call_args.args[1] == RULES
    assert classify.call_args.args[4] == '测试部'


def test_adjustment_allows_separate_level():
    rules = [RULES[0] | {"adjustment": "重新设计可调B"}]
    result = RATED | {"level": "B", "reason": "需要重新设计"}
    assert validate_result(result, OPTIONS, rules, '测试部')['level'] == 'B'
