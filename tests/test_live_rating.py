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
    # 标题复用实际需求文本，不将测试标签或预期结论传给模型。
    result = client.classify({'title': case['description'], 'description': case['description'],
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
