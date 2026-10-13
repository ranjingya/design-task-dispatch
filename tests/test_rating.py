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
           "department": "部门", "load_score": "分数", "work_type": "类别", "level": "等级", "rule_explanation": "结果"}
RULES = [{"record_id": "recRule", "work_type": "测试类型", "content": "测试工作", "departments": ["测试部"],
          "default_level": "A", "adjustment": "", "assignment": "轮流", "load_score": 2}]
RAW_RULES = [{"record_id": "recRule", "fields": {"工作类型": "测试类型", "包含的工作": "测试工作",
              "适用部门": "测试部", "默认ABC分类": "A基础重复", "分类调整说明": "", "推荐方式": "轮流", "人员负载分数": 2}}]
OPTIONS = ["测试类型"]
SCHEMA = [{"field_name": "部门", "type": 3}, {"field_name": "平台", "type": 1},
          {"field_name": "分数", "type": 2}, {"field_name": "类别", "type": 3, "property": {"options": [{"name": x} for x in OPTIONS]}},
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
                    rules_cache_path=tmp_path / "rules.json", rules_table_id="rules", ai_key="private-key", ai_model="test-model",
                    prompt_path=ROOT / "prompts/classify.md")


@pytest.fixture
def feishu(settings):
    client = Mock(spec=FeishuClient)
    client.settings = settings
    client.get_fields.return_value = SCHEMA
    client.get_rules.return_value = RAW_RULES
    client.get_record.return_value = {"标题": "测试", "说明": "测试需求", "平台": "示例平台", "部门": "测试部"}
    return client


def test_success_writes_rating_and_score(feishu, monkeypatch):
    classify = Mock(return_value=RATED)
    monkeypatch.setattr(AIClient, "classify", classify)
    feishu.get_record.return_value.update({"类别": "旧类别", "结果": "旧原因", "最终负责人": "某人"})
    process_record("recTest", MAPPING, feishu)
    fields = feishu.update_record.call_args.args[1]
    assert set(fields) == {"类别", "等级", "结果", "分数"}
    assert fields["类别"] == "测试类型"
    assert fields["结果"] == "已匹配｜测试类型\n\n对应：测试工作"
    assert classify.call_args.args[0]["platform"] == "示例平台"
    assert fields["等级"] == "A"
    assert fields["分数"] == 2


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
    assert "待确认｜测试类型" in feishu.update_record.call_args.args[1]["结果"]


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
    assert "待确认｜测试类型" in updates["结果"] and "用途不清晰" in updates["结果"]


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
    assert select_department("测试部", RULES + [other]) == ("测试部", RULES)
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
    assert output == "已匹配｜测试类型\n\n对应：另一工作"


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


@pytest.mark.parametrize('departments', ['测试部、其他部', ['测试部', '其他部'],
                                          [{'text': '测试部、'}, {'text': '其他部'}]])
def test_multiselect_and_legacy_department_rules(departments):
    raw = [{'record_id': 'recRule', 'fields': RAW_RULES[0]['fields'] |
            {'工作类型': ['测试类型'], '适用部门': departments, '默认ABC分类': ['A']}}]
    rules = normalize_rules(raw, ROOT / 'config/rule_fields.json')
    assert rules[0]['departments'] == ['测试部', '其他部']
    assert rules[0]['work_type'] == '测试类型'
    assert select_department(['其他部'], rules) == ('其他部', rules)


@pytest.mark.parametrize('value', [None, '', '测试部-平台', ['测试部', '其他部'], '不存在'])
def test_department_requires_exact_single_option(value):
    assert select_department(value, RULES)[0] is None


def test_department_wins_over_platform(feishu, monkeypatch):
    feishu.get_record.return_value['平台'] = '其他部-某平台'
    classify = Mock(return_value=RATED)
    monkeypatch.setattr(AIClient, 'classify', classify)
    process_record('recTest', MAPPING, feishu)
    assert classify.call_args.args[4] == '测试部'
    assert classify.call_args.args[0]['department'] == '测试部'


def test_duplicate_type_score_comes_from_exact_rule_and_not_level(feishu, monkeypatch):
    second = {'record_id': 'recSecond', 'fields': RAW_RULES[0]['fields'] |
              {'包含的工作': '另一工作', '人员负载分数': 7.5, '分类调整说明': '重新设计可调B'}}
    feishu.get_rules.return_value = RAW_RULES + [second]
    monkeypatch.setattr(AIClient, 'classify', Mock(return_value=RATED |
                        {'rule_id': 'recSecond', 'level': 'B', 'load_score': 999}))
    process_record('recTest', MAPPING, feishu)
    assert feishu.update_record.call_args.args[1]['分数'] == 7.5
    assert feishu.update_record.call_args.args[1]['等级'] == 'B'


def test_unmatched_clears_previous_load_score(feishu, monkeypatch):
    feishu.get_record.return_value['分数'] = 9
    monkeypatch.setattr(AIClient, 'classify', Mock(return_value=REVIEW))
    process_record('recTest', MAPPING, feishu)
    assert feishu.update_record.call_args.args[1]['分数'] is None


@pytest.mark.parametrize('score', [None, -1, True, '3分', '', 'NaN', '1e999', float('nan'), float('inf')])
def test_invalid_rule_score_never_becomes_zero(feishu, monkeypatch, score):
    feishu.get_rules.return_value = [{'record_id': 'recRule', 'fields':
                                     RAW_RULES[0]['fields'] | {'人员负载分数': score}}]
    monkeypatch.setattr(AIClient, 'classify', Mock(return_value=RATED))
    process_record('recTest', MAPPING, feishu)
    updates = feishu.update_record.call_args.args[1]
    assert updates['类别'] == '测试类型'
    assert updates['分数'] is None
    assert '规则分数缺失或无效' in updates['结果']


def test_zero_score_is_valid(feishu, monkeypatch):
    feishu.get_rules.return_value = [{'record_id': 'recRule', 'fields':
                                     RAW_RULES[0]['fields'] | {'人员负载分数': 0}}]
    monkeypatch.setattr(AIClient, 'classify', Mock(return_value=RATED))
    process_record('recTest', MAPPING, feishu)
    assert feishu.update_record.call_args.args[1]['分数'] == 0


@pytest.mark.parametrize('field,kind', [('分数', 19), ('部门', 1), ('平台', 3)])
def test_changed_input_or_score_schema_fails_before_ai(feishu, monkeypatch, field, kind):
    feishu.get_fields.return_value = [f | {'type': kind} if f['field_name'] == field else f for f in SCHEMA]
    classify = Mock()
    monkeypatch.setattr(AIClient, 'classify', classify)
    with pytest.raises(ValueError):
        process_record('recTest', MAPPING, feishu)
    classify.assert_not_called()
    feishu.update_record.assert_not_called()


@pytest.mark.parametrize('raw,expected', [('4', 4), ('7.5', 7.5), ('0', 0), (' 2 ', 2)])
def test_feishu_numeric_string_score(feishu, monkeypatch, raw, expected):
    feishu.get_rules.return_value = [{'record_id': 'recRule', 'fields':
                                     RAW_RULES[0]['fields'] | {'人员负载分数': raw}}]
    monkeypatch.setattr(AIClient, 'classify', Mock(return_value=RATED))
    process_record('recTest', MAPPING, feishu)
    assert feishu.update_record.call_args.args[1]['分数'] == expected


def test_recommendation_integrates_single_write(feishu, monkeypatch):
    mapping = MAPPING | {'recommendation_result': '推荐结果', 'recommended_assignee': '推荐人',
                         'assignee': '负责人', 'leader_confirmed': '确认', 'completed_at': '完成',
                         'cancelled': '取消', 'is_test': '测试'}
    feishu.get_fields.return_value = SCHEMA + [{'field_name': '推荐结果', 'type': 1}, {'field_name': '推荐人', 'type': 11}]
    monkeypatch.setattr(AIClient, 'classify', Mock(return_value=RATED))
    recommend = Mock(return_value=({'推荐结果': '依据', '推荐人': [{'id': 'a'}]}, None))
    monkeypatch.setattr('app.dispatch.recommend', recommend)
    process_record('recTest', mapping, feishu)
    feishu.update_record.assert_called_once()
    updates = feishu.update_record.call_args.args[1]
    assert updates['分数'] == 2 and updates['推荐人'] == [{'id': 'a'}]
    assert recommend.call_args.args[3]['record_id'] == 'recRule'

    feishu.reset_mock()
    feishu.get_record.side_effect = [{'标题': '测试', '说明': '需求', '部门': '测试部'}, {'确认': True}]
    process_record('recTest', mapping, feishu)
    feishu.update_record.assert_not_called()


def test_recommendation_failure_preserves_rating_and_clears_old_recommendation(feishu, monkeypatch):
    mapping = MAPPING | {'recommendation_result': '推荐结果', 'recommended_assignee': '推荐人'}
    feishu.get_fields.return_value = SCHEMA + [{'field_name': '推荐结果', 'type': 1}, {'field_name': '推荐人', 'type': 11}]
    monkeypatch.setattr(AIClient, 'classify', Mock(return_value=RATED))
    monkeypatch.setattr('app.dispatch.recommend', Mock(side_effect=RuntimeError('来源不可读')))
    process_record('recTest', mapping, feishu)
    updates = feishu.update_record.call_args.args[1]
    assert updates['分数'] == 2 and updates['推荐人'] == [] and updates['推荐结果'] == '人工分配\n\n资源读取或配置校验失败，请重试\n\n人员负载暂不可用\n\n本任务 2 分'


def test_compact_match_adjustment_and_duplicate_candidates():
    output = format_result(RATED | {'level': 'B', 'reason': '需要重新设计\n版式'}, RULES)
    assert output == '已匹配｜测试类型\n\n对应：测试工作\n\n调整为 B：需要重新设计 版式'
    second = RULES[0] | {'record_id': 'second', 'content': '另一工作'}
    result = REVIEW | {'candidates': REVIEW['candidates'] + [REVIEW['candidates'][0] | {'rule_id': 'second'}]}
    output = format_result(result, RULES + [second])
    assert '测试类型（测试工作） / 测试类型（另一工作）' in output
    assert 'recRule' not in output and 'second' not in output
