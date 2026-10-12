"""通过真实模型验证业务样例；显式开启后产生 API 调用，不写入飞书。"""

import json
import os

import pytest

from app.ai import AIClient
from app.config import ROOT, Settings
from app.feishu import FeishuClient
from app.rules import normalize_rules, select_department

CASES = json.loads((ROOT / 'tests/fixtures/rating_cases.json').read_text(encoding='utf-8'))
pytestmark = pytest.mark.skipif(os.getenv('LIVE_AI_TESTS') != '1', reason='需要显式开启真实模型测试')


@pytest.fixture(scope='module')
def live_context():
    settings = Settings.from_env()
    settings.validate_ai()
    client = FeishuClient(settings)
    rules = normalize_rules(client.get_rules(), settings.rule_fields_path)
    options = [o['name'] for f in client.get_fields() if f['field_name'] == '工作类型'
               for o in f['property']['options']]
    return AIClient(settings), rules, options


@pytest.mark.parametrize('case', CASES, ids=[f'scenario_{i + 1}' for i in range(len(CASES))])
def test_real_model_matching(case, live_context):
    client, rules, options = live_context
    department, eligible = select_department(case['platform'], rules)
    # 使用实际联调时的标题与备注，验证任务管理标记不干扰业务判断。
    result = client.classify({'title': case.get('title_prefix', '') + case['description'],
                              'description': case['description'], 'notes': case.get('notes', ''),
                              'platform': case['platform'], 'material_issues': []},
                             eligible, options, [], department)
    assert result['matched'] == (case['expected_type'] is not None), result
    assert result['work_type'] == case['expected_type'], result
    assert result['level'] == case['expected_level'], result
    if result['matched']:
        rule = next(r for r in eligible if r['record_id'] == result['rule_id'])
        assert case['rule_content'] in rule['content'], result
    else:
        assert result['unmatched_reason']
        assert len(result['candidates']) >= case['min_candidates'], result

        if 'expected_candidate_contents' in case:
            actual_contents = sorted(next(r['content'] for r in eligible if r['record_id'] == c['rule_id'])
                                     for c in result['candidates'])
            assert actual_contents == sorted(case['expected_candidate_contents']), result
        if case.get('missing_information_only_department'):
            assert result['missing_information'], result
            assert all('部门' in item for item in result['missing_information']), result
            assert not any(word in ' '.join(result['missing_information'])
                           for word in ('直播', 'GIF', '贴片', '使用场景')), result
    explanations = [result.get('reason') or '', result.get('unmatched_reason') or '',
                    *result['missing_information']]
    for candidate in result['candidates']:
        explanations.extend([candidate['match_reason'], candidate['uncertainty']])
    explanation = ' '.join(explanations)
    for term in ('allowed_work_types', 'confirmed_department', 'record_id', 'content',
                 'departments', 'adjustment', 'default_level', 'work_type',
                 '联调', '验收', '测试', '真实任务', '真实设计', '正式评级'):
        assert term not in explanation, result
    assert not any(rule['record_id'] in explanation for rule in eligible), result
