"""店铺简称、平台归一化、模型消歧和人工兜底测试。"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import ROOT
from app.shop_matching import normalize, resolve_shop


@pytest.fixture
def config():
    return {'shop_platforms': {'平台甲': ['AA'], '平台乙': ['BB']}, 'shop_ai_fallback': True,
            'shops': [
                {'id': 'one', 'name': '平台甲儿童服饰旗舰店', 'department': '部门甲', 'platform': '平台甲',
                 'aliases': ['平台甲儿童服饰旗舰店', '平台甲服饰店'], 'primary': '甲', 'backup': []},
                {'id': 'two', 'name': '平台甲雨具店', 'department': '部门甲', 'platform': '平台甲',
                 'aliases': ['平台甲雨具店', '雨具店'], 'primary': '乙', 'backup': []},
                {'id': 'three', 'name': '平台乙服饰店', 'department': '部门甲', 'platform': '平台乙',
                 'aliases': ['平台乙服饰店', '服饰店'], 'primary': '丙', 'backup': []},
            ]}


@pytest.fixture
def model(monkeypatch):
    complete = Mock(return_value=({'status': 'matched', 'shop_id': 'one', 'reason': '童装简称对应儿童服饰店',
                                 'evidence': {'source': 'description', 'quote': '童装那家店'}}, {}))
    monkeypatch.setattr('app.shop_matching.AIClient._complete', complete)
    return complete


@pytest.fixture
def settings():
    return SimpleNamespace(prompt_path=ROOT / 'prompts/classify.md', validate_ai=Mock())


def test_normalize_fullwidth_punctuation_and_ascii_boundary():
    platforms = {'平台甲': ['AA']}
    assert normalize('ＡＡ－服饰 店', platforms) == '平台甲服饰店'
    assert normalize('xAAx', platforms) == 'xaax'


def test_direct_match_and_department_filter(config, model, settings):
    config['shops'][2]['aliases'] = ['平台乙服饰店']
    shop, reason, _ = resolve_shop({'department': '部门甲', 'platform': 'ＡＡ儿童服饰旗舰店', 'title': '页面装修'}, config, settings)
    assert shop['id'] == 'one' and '平台字段' in reason
    model.assert_not_called()
    assert resolve_shop({'department': '部门乙', 'title': '平台甲服饰店装修'}, config, settings)[0] is None


def test_short_name_requires_ai_even_when_unique(config, model, settings):
    model.return_value[0]['evidence'] = {'source': 'platform', 'quote': 'AA服饰店'}
    shop, _, _ = resolve_shop({'department': '部门甲', 'platform': 'AA服饰店'}, config, settings)
    assert shop['id'] == 'one'
    model.assert_called_once()


def test_cross_platform_reference_requires_context(config, model, settings):
    model.return_value = ({'status': 'matched', 'shop_id': 'three', 'reason': '乙店为目标，甲店仅参考',
                          'evidence': {'source': 'description', 'quote': '给平台乙服饰店做页面'}}, {})
    shop, _, _ = resolve_shop({'department': '部门甲', 'platform': 'BB',
                              'title': '参考平台甲服饰店', 'description': '给平台乙服饰店做页面'}, config, settings)
    assert shop['id'] == 'three'
    model.assert_called_once()


def test_multiple_platforms_stays_manual(config, model, settings):
    assert resolve_shop({'department': '部门甲', 'platform': 'AA、BB', 'title': '服饰店装修'}, config, settings)[0] is None
    model.assert_not_called()


def test_ai_selects_existing_id_with_original_evidence(config, model, settings):
    shop, reason, candidates = resolve_shop({'department': '部门甲', 'platform': 'AA', 'description': '童装那家店'}, config, settings)
    assert shop['id'] == 'one' and '童装那家店' in reason
    payload = json.loads(model.call_args.args[0][1]['content'])
    assert {s['id'] for s in payload['candidates']} == {'one', 'two'}
    assert all('primary' not in s and 'backup' not in s for s in payload['candidates'])


def test_document_evidence_is_available_but_requires_context(config, model, settings):
    model.return_value[0]['evidence'] = {'source': 'material_0_text', 'quote': '平台甲儿童服饰旗舰店'}
    shop, _, _ = resolve_shop({'department': '部门甲', 'platform': 'AA', 'title': '首页更新',
                              'materials': [{'source': '需求脚本', 'text': '本次目标是平台甲儿童服饰旗舰店。'}]}, config, settings)
    assert shop['id'] == 'one'
    assert model.call_count == 1


@pytest.mark.parametrize('change', [
    {'shop_id': 'invented'}, {'shop_id': 'three'},
    {'evidence': {'source': 'description', 'quote': '捏造原文'}},
    {'evidence': {'source': 'platform', 'quote': 'AA'}},
    {'evidence': {'source': 'description', 'quote': 'AA'}},
    {'status': 'unknown'}, {'reason': ''}, {'evidence': None},
])
def test_bad_ai_output_stays_manual(config, model, settings, change):
    model.return_value = (model.return_value[0] | change, {})
    shop, reason, candidates = resolve_shop({'department': '部门甲', 'platform': 'AA', 'description': 'AA 童装那家店'}, config, settings)
    assert shop is None and '人工确认' in reason and len(candidates) == 2


def test_review_and_model_timeout_are_explained(config, model, settings):
    model.return_value = ({'status': 'needs_review', 'shop_id': None, 'reason': '只有平台，请确认服饰店还是雨具店', 'evidence': None}, {})
    demand = {'department': '部门甲', 'platform': 'AA', 'title': '首页装修'}
    assert '服饰店还是雨具店' in resolve_shop(demand, config, settings)[1]
    model.side_effect = RuntimeError('超时')
    assert '暂不可用' in resolve_shop(demand, config, settings)[1]


def test_no_ai_when_disabled_or_materials_incomplete(config, model, settings):
    demand = {'department': '部门甲', 'title': '童装那家店', 'material_issues': ['缺页']}
    assert resolve_shop(demand, config, settings)[0] is None
    config['shop_ai_fallback'] = False
    demand.pop('material_issues')
    assert resolve_shop(demand, config, settings)[0] is None
    model.assert_not_called()


def test_invalid_catalog_rejected(config, settings):
    config['shops'][1]['id'] = 'one'
    with pytest.raises(ValueError, match='标识重复'):
        resolve_shop({}, config, settings)


def test_current_demand_resolves_to_configured_owner():
    config = json.loads((ROOT / 'config/recommendation.json').read_text())
    shop, _, _ = resolve_shop({'department': '市场部', 'platform': 'ＰＤＤ儿童服饰旗舰店'}, config)
    assert shop['name'] == '拼多多儿童服饰旗舰店'
    assert shop['primary'] == '蛋糕' and shop['backup'] == ['塔可']


@pytest.mark.parametrize('field', ['title', 'description', 'notes', 'platform'])
def test_reference_or_negated_alias_never_direct_matches(config, model, settings, field):
    model.return_value = ({'status': 'needs_review', 'shop_id': None, 'reason': '仅有参考店铺，缺少目标', 'evidence': None}, {})
    shop, reason, _ = resolve_shop({'department': '部门甲', field: '不要分给平台甲儿童服饰旗舰店，只是参考'}, config, settings)
    assert shop is None and '缺少目标' in reason
    model.assert_called_once()


def test_title_platform_does_not_hide_target_candidates(config, model, settings):
    resolve_shop({'department': '部门甲', 'title': '参考平台甲服饰店', 'description': '童装那家店'}, config, settings)
    payload = json.loads(model.call_args.args[0][1]['content'])
    assert {shop['id'] for shop in payload['candidates']} == {'one', 'two', 'three'}
    assert payload['confirmed_platform'] is None


def test_title_alias_with_ai_disabled_stays_manual(config):
    config['shop_ai_fallback'] = False
    assert resolve_shop({'department': '部门甲', 'title': '平台甲儿童服饰旗舰店装修'}, config)[0] is None
