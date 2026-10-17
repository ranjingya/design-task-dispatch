"""人员推荐、归属匹配、负载预占及人工保护测试。"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import ROOT
from app.recommendation import workloads, product_owner, shop_owner, recommend, save_rotation, protected

M = {'recommended_assignee': '推荐人', 'assignee': '负责人', 'leader_confirmed': '确认',
     'completed_at': '完成', 'cancelled': '取消', 'load_score': '分数',
     'recommendation_result': '说明'}
P = {'code_pattern': r'(?<![A-Z0-9])[A-Z]{1,5}[0-9]{4,12}(?![A-Z0-9])',
     'bundle_pattern': '拼款', 'old_pattern': '老款', 'platform_aliases': ['平台甲'],
     'title_field': '商品', 'owner_field': '认领'}


def row(rid, **fields):
    return {'record_id': rid, 'fields': fields}


def test_workloads_reservation_confirmation_and_exclusion():
    records = [row('self', 推荐人=[{'id': 'a'}], 分数=99),
               row('pending', 推荐人=[{'id': 'a'}], 分数='4'),
               row('confirmed', 推荐人=[{'id': 'a'}], 负责人=[{'id': 'b'}], 确认=True, 分数=7),
               row('history', 标题='【历史验证】任务', 推荐人=[{'id': 'a'}], 分数=3),
               row('cancel', 推荐人=[{'id': 'a'}], 取消=True, 分数=99),
               row('complete', 负责人=[{'id': 'b'}], 确认=True, 完成=123, 分数=99),
               row('missing', 推荐人=[{'id': 'b'}])]
    result = workloads(records, M, ['a', 'b'], 'self')
    assert result['a'] == {'score': 7, 'count': 2, 'missing': []}
    assert result['b'] == {'score': 7, 'count': 2, 'missing': ['missing']}


def test_product_single_bundle_platform_and_conflict():
    records = [row('single', 商品='XX12345 单款', 认领='甲'),
               row('bundle', 商品='XX12345+XX12346（平台甲拼款）', 认领='乙')]
    assert product_owner({'title': 'XX12345 改图'}, records, P)[0] == '甲'
    assert product_owner({'title': 'XX12345+XX12346 拼款', 'platform': '平台甲'}, records, P)[0] == '乙'
    assert product_owner({'title': 'XX12345+XX12346 拼款'}, records, P)[0] is None
    assert product_owner({'title': 'XX12345 拼款'}, records, P)[0] is None
    records.append(row('conflict', 商品='XX12345', 认领='乙'))
    assert product_owner({'title': 'XX12345'}, records, P)[0] is None
    assert product_owner({'title': 'XX123456'}, records, P)[0] is None
    assert product_owner({'title': '老款 XX99999'}, records, P)[2] is True
    assert product_owner({'title': '老款'}, records, P)[2] is False


def test_shop_requires_unique_department_and_store():
    shops = [{'department': '部门甲', 'aliases': ['店铺一']}, {'department': '部门甲', 'aliases': ['店铺二']}]
    assert shop_owner({'department': '部门甲', 'platform': '店铺一'}, shops) == shops[0]
    assert shop_owner({'department': '部门乙', 'platform': '店铺一'}, shops) is None
    assert shop_owner({'department': '部门甲', 'title': '店铺一和店铺二'}, shops) is None


@pytest.fixture
def client(tmp_path):
    config = {'members': {'甲': 'a', '乙': 'b'}, 'rotation': ['甲', '乙'],
              'member_table': 'members', 'member_field': '人员', 'excluded_owners': ['退组人员'],
              'ownership_routes': {'product': 'product', 'shop': 'shop'},
              'product': P | {'base_token': 'external', 'table_id': 'products'},
              'shops': [{'department': '部门甲', 'aliases': ['店铺一'], 'primary': '甲', 'backup': ['乙']}]}
    member_table = config.pop('member_table')
    product_base = config['product'].pop('base_token')
    product_table = config['product'].pop('table_id')
    (tmp_path / 'recommendation.json').write_text(json.dumps(config))
    f = Mock()
    f.settings = SimpleNamespace(fields_path=tmp_path / 'fields.json', rules_cache_path=tmp_path / 'cache/rules.json', table_id='tasks',
                                 members_table_id=member_table,
                                 product_base_token=product_base, product_table_id=product_table)
    f.data = {'members': [row('a', 人员=[{'id': 'a'}]), row('b', 人员=[{'id': 'b'}])], 'tasks': [], 'products': []}
    f.get_records.side_effect = lambda table, *args: f.data[table]
    return f


def run(client, method='轮流', rid='r', demand=None, fields=None):
    return recommend('self', fields or {}, demand or {},
                     {'record_id': rid, 'assignment': method, 'load_score': 4}, M, client)


def test_rotation_persists_only_after_write_and_rerun_stable(client):
    updates, pending = run(client)
    assert updates['推荐人'] == [{'id': 'a'}]
    assert not pending[0].exists()
    save_rotation(pending)
    assert run(client)[0]['推荐人'] == [{'id': 'b'}]
    assert run(client, fields={'推荐人': [{'id': 'a'}]})[0]['推荐人'] == [{'id': 'a'}]
    assert run(client, fields={'标题': '【历史验证】任务'})[1] is not None


def test_minimum_load_fixed_pool_and_missing_score(client):
    client.data['tasks'] = [row('busy', 推荐人=[{'id': 'a'}], 分数=8)]
    assert run(client)[0]['推荐人'] == [{'id': 'b'}]
    assert run(client, '固定人员（甲）')[0]['推荐人'] == [{'id': 'a'}]
    client.data['tasks'].append(row('missing', 推荐人=[{'id': 'b'}]))
    assert run(client)[0]['推荐人'] == [{'id': 'b'}]
    assert '乙 0（1 条待计分）' in run(client)[0]['说明']
    assert run(client, '固定人员（乙）')[0]['推荐人'] == [{'id': 'b'}]
    assert run(client, '固定人员（未知）')[0]['推荐人'] == []


@pytest.mark.parametrize('score', [None, -1, '无效', True, float('nan'), float('inf')])
def test_confirmed_missing_score_does_not_exclude_its_assignee(client, score, caplog):
    """已确认任务缺分时，该任务暂不计分，负责人仍参与负载比较。"""
    client.data['tasks'] = [row('unknown', 负责人=[{'id': 'a'}], 确认=True, 分数=score),
                            row('known', 负责人=[{'id': 'b'}], 确认=True, 分数=6)]
    updates, pending = run(client)
    assert updates['推荐人'] == [{'id': 'a'}]
    assert updates['说明'].split('\n\n')[2] == '甲 0（1 条待计分） ｜ 乙 6'
    assert updates['说明'].split('\n\n')[1] == '全体美工负载最低优先，同分轮流'
    assert pending is not None
    assert '负载统计忽略缺分任务' in caplog.text and '甲=unknown' in caplog.text


def test_unassigned_missing_tasks_do_not_reserve_any_person(client):
    """没有最终负责人或推荐人的缺分任务不影响人员负载。"""
    client.data['tasks'] = [row('unassigned'), row('unassigned-confirmed', 确认=True)]
    updates, _ = run(client)
    assert updates['推荐人'] == [{'id': 'a'}]
    assert updates['说明'].split('\n\n')[2] == '甲 0 ｜ 乙 0'


def test_all_candidates_missing_score_still_rotate(client):
    """候选仅有缺分任务时，按已计分负载为零正常轮换。"""
    client.data['tasks'] = [row('unknown', 推荐人=[{'id': 'a'}, {'id': 'b'}])]
    updates, pending = run(client)
    assert updates['推荐人'] == [{'id': 'a'}] and pending is not None
    assert updates['说明'].split('\n\n') == [
        '轮流', '全体美工负载最低优先，同分轮流',
        '甲 0（1 条待计分） ｜ 乙 0（1 条待计分）', '本任务 4 分']
    save_rotation(pending)
    assert run(client)[0]['推荐人'] == [{'id': 'b'}]


def test_missing_task_joins_load_after_score_is_supplied(client):
    """缺分任务补充分数后，下一次计算自动计入，零分也属于有效分数。"""
    client.data['tasks'] = [row('unknown', 推荐人=[{'id': 'a'}]),
                            row('known', 推荐人=[{'id': 'b'}], 分数=6)]
    updates, pending = run(client)
    assert updates['推荐人'] == [{'id': 'a'}]
    save_rotation(pending)
    client.data['tasks'][0]['fields']['分数'] = 8
    assert run(client)[0]['推荐人'] == [{'id': 'b'}]
    client.data['tasks'][0]['fields']['分数'] = 0
    updates, _ = run(client)
    assert updates['推荐人'] == [{'id': 'a'}]
    assert updates['说明'].split('\n\n')[2] == '甲 0 ｜ 乙 6'


@pytest.mark.parametrize('known_score,expected', [(5, 'a'), (8, 'b')])
def test_missing_task_preserves_same_persons_other_load(client, known_score, expected):
    """忽略缺分任务时，保留同一人员的已确认负载与待确认预占。"""
    client.data['tasks'] = [row('unknown', 负责人=[{'id': 'a'}], 确认=True),
                            row('confirmed', 负责人=[{'id': 'a'}], 确认=True, 分数=known_score - 2),
                            row('pending', 推荐人=[{'id': 'a'}], 分数=2),
                            row('other', 负责人=[{'id': 'b'}], 确认=True, 分数=6)]
    updates, _ = run(client)
    assert updates['推荐人'] == [{'id': expected}]
    assert updates['说明'].split('\n\n')[2] == f'甲 {known_score}（1 条待计分） ｜ 乙 6'
    assert workloads(client.data['tasks'], M, ['a', 'b'], 'self')['a'] == {
        'score': known_score, 'count': 3, 'missing': ['unknown']}


@pytest.mark.parametrize('score', [None, -1, '无效', True])
def test_current_rule_still_requires_valid_score(client, score):
    """本次规则分数无效时保留人工分配，不进入人员负载比较。"""
    updates, pending = recommend('self', {}, {}, {'assignment': '轮流', 'load_score': score}, M, client)
    assert updates['推荐人'] == [] and pending is None
    assert '规则负载分缺失或无效' in updates['说明']
    client.get_records.assert_not_called()


def test_fixed_owner_still_applies_when_its_load_is_unknown(client):
    """按商品归属直接指定的人员保持归属关系，说明中展示待计分提示。"""
    client.data['products'] = [row('p', 商品='XX12345', 认领='甲')]
    client.data['tasks'] = [row('unknown', 推荐人=[{'id': 'a'}])]
    updates, pending = run(client, '找对应人员', 'product', {'title': 'XX12345'})
    assert updates['推荐人'] == [{'id': 'a'}] and pending is None
    assert updates['说明'].split('\n\n')[2] == '甲 0（1 条待计分）'


def test_manual_unconfigured_and_excluded_owner(client):
    assert run(client, '人工分配')[0]['推荐人'] == []
    assert run(client, '')[0]['推荐人'] == []
    assert run(client, '找对应人员')[0]['推荐人'] == []
    client.data['products'] = [row('p', 商品='XX12345', 认领='退组人员')]
    updates, _ = run(client, '找对应人员', 'product', {'title': 'XX12345'})
    assert updates['推荐人'] == [] and '人工分配' in updates['说明']


def test_shop_owner_preserved_despite_load(client):
    client.data['tasks'] = [row('busy', 推荐人=[{'id': 'a'}], 分数=80)]
    updates, _ = run(client, '找对应人员', 'shop', {'department': '部门甲', 'platform': '店铺一'})
    assert updates['推荐人'] == [{'id': 'a'}, {'id': 'b'}]
    assert updates['说明'].split('\n\n')[2] == '甲 80 ｜ 乙 0'
    assert '主副负责人' in updates['说明']


@pytest.mark.parametrize('fields', [{'负责人': [{'id': 'a'}]}, {'确认': True}, {'取消': True}, {'完成': 123}])
def test_protected_records(fields):
    assert protected(fields, M)


def test_configuration_routes_and_members_consistent():
    config = json.loads((ROOT / 'config/recommendation.json').read_text())
    assert len(config['shops']) == 10
    assert len(config['rotation']) == len(set(config['rotation']))
    assert config['members'] and config['ownership_routes'] and 'member_table' not in config
    assert 'base_token' not in config['product'] and 'table_id' not in config['product']
    for shop in config['shops']:
        assert all(n in config['rotation'] for n in [shop['primary']] + shop['backup'])


@pytest.mark.parametrize('name', ['members_table_id', 'product_base_token', 'product_table_id'])
def test_missing_resource_config_does_not_recommend(client, name):
    setattr(client.settings, name, '')
    with pytest.raises(ValueError, match='请配置'):
        run(client, '找对应人员', 'product', {'title': 'XX12345'})


def test_resource_ids_only_come_from_settings(client):
    client.settings.members_table_id = 'replacement-members'
    client.settings.product_base_token = 'replacement-base'
    client.settings.product_table_id = 'replacement-products'
    client.data['replacement-members'] = client.data.pop('members')
    client.data['replacement-products'] = [row('p', 商品='XX12345', 认领='甲')]
    assert run(client, '找对应人员', 'product', {'title': 'XX12345'})[0]['推荐人'] == [{'id': 'a'}]
    client.get_records.assert_any_call('replacement-members')
    client.get_records.assert_any_call('replacement-products', 'replacement-base')


@pytest.mark.parametrize('key,value', [('members', {}), ('members', {'甲': None}),
                                       ('ownership_routes', []), ('ownership_routes', {'r': 'unknown'})])
def test_invalid_business_mapping_rejected(client, key, value):
    path = client.settings.fields_path.parent / 'recommendation.json'
    config = json.loads(path.read_text())
    config[key] = value
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match=key):
        run(client)


def test_business_config_reloaded_each_task(client):
    path = client.settings.fields_path.parent / 'recommendation.json'
    assert run(client)[0]['推荐人'] == [{'id': 'a'}]
    config = json.loads(path.read_text())
    config['rotation'] = ['乙', '甲']
    path.write_text(json.dumps(config))
    assert run(client)[0]['推荐人'] == [{'id': 'b'}]


@pytest.fixture
def platform_client(client):
    path = client.settings.fields_path.parent / 'recommendation.json'
    config = json.loads(path.read_text())
    config['ownership_routes']['creative'] = 'product_platform'
    config['product_platform_rules'] = {'creative': [
        {'aliases': ['平台甲', 'AA'], 'action': 'owner'},
        {'aliases': ['平台乙', 'BB'], 'action': 'exclude_owner'}]}
    config['product']['platform_short_names'] = {'平台甲': ['AA']}
    path.write_text(json.dumps(config))
    client.data['products'] = [row('p', 商品='XX12345', 认领='甲')]
    return client


@pytest.mark.parametrize('platform,expected', [('平台甲', 'a'), ('aa', 'a'), ('平台乙旗舰店', 'b'), ('bb', 'b')])
def test_platform_owner_or_exclusion(platform_client, platform, expected):
    updates, state = run(platform_client, '找对应人员', 'creative', {'title': 'XX12345', 'platform': platform})
    assert updates['推荐人'] == [{'id': expected}]
    assert (state is None) == (expected == 'a')
    if expected == 'b':
        assert '排除原认领人甲' in updates['说明']


@pytest.mark.parametrize('platform', ['', '未知平台', '平台甲、平台乙', 'aabb'])
def test_platform_unclear_stays_manual(platform_client, platform):
    assert run(platform_client, '找对应人员', 'creative', {'title': 'XX12345', 'platform': platform})[0]['推荐人'] == []


@pytest.mark.parametrize('owner', ['退组人员', '未配置', ''])
@pytest.mark.parametrize('platform', ['平台甲', '平台乙'])
def test_platform_invalid_owner_stays_manual(platform_client, owner, platform):
    platform_client.data['products'][0]['fields']['认领'] = owner
    assert run(platform_client, '找对应人员', 'creative', {'title': 'XX12345', 'platform': platform})[0]['推荐人'] == []


def test_platform_old_product_does_not_fallback(platform_client):
    assert run(platform_client, '找对应人员', 'creative', {'title': '老款 XX99999', 'platform': '平台乙'})[0]['推荐人'] == []


def test_excluded_owner_missing_load_is_not_candidate(platform_client):
    platform_client.data['tasks'] = [row('missing', 推荐人=[{'id': 'a'}])]
    assert run(platform_client, '找对应人员', 'creative', {'title': 'XX12345', 'platform': '平台乙'})[0]['推荐人'] == [{'id': 'b'}]
    platform_client.data['tasks'].append(row('candidate-missing', 推荐人=[{'id': 'b'}]))
    assert run(platform_client, '找对应人员', 'creative', {'title': 'XX12345', 'platform': '平台乙'})[0]['推荐人'] == [{'id': 'b'}]


def test_platform_alias_applies_to_product_limits(platform_client):
    platform_client.data['products'] = [row('p', 商品='XX12345+XX12346 平台甲拼款', 认领='甲')]
    assert run(platform_client, '找对应人员', 'creative', {'title': 'XX12345+XX12346', 'platform': 'AA'})[0]['推荐人'] == [{'id': 'a'}]


def test_platform_exclusion_load_priority_and_rotation(platform_client):
    path = platform_client.settings.fields_path.parent / 'recommendation.json'
    config = json.loads(path.read_text())
    config['members']['丙'] = 'c'
    config['rotation'].append('丙')
    path.write_text(json.dumps(config))
    platform_client.data['members'].append(row('c', 人员=[{'id': 'c'}]))
    demand = {'title': 'XX12345', 'platform': '平台乙'}
    first, state = run(platform_client, '找对应人员', 'creative', demand)
    assert first['推荐人'] == [{'id': 'b'}]
    save_rotation(state)
    assert run(platform_client, '找对应人员', 'creative', demand)[0]['推荐人'] == [{'id': 'c'}]
    platform_client.data['tasks'] = [row('busy', 推荐人=[{'id': 'c'}], 分数=10)]
    assert run(platform_client, '找对应人员', 'creative', demand)[0]['推荐人'] == [{'id': 'b'}]


@pytest.mark.parametrize('method,rid,demand,heading', [
    ('轮流', 'r', {}, '轮流'),
    ('固定人员（甲 乙）', 'r', {}, '固定人员'),
    ('人工分配', 'r', {}, '人工分配'),
    ('找对应人员', 'unknown', {}, '人工分配'),
    ('找对应人员', 'shop', {'department': '部门甲', 'platform': '店铺一'}, '对应人员'),
    ('找对应人员', 'shop', {}, '人工分配'),
    ('固定人员（未知）', 'r', {}, '人工分配'),
])
def test_recommendation_four_line_display(client, method, rid, demand, heading):
    updates, _ = run(client, method, rid, demand)
    lines = updates['说明'].split('\n\n')
    assert len(lines) == 4 and lines[0] == heading and lines[3] == '本任务 4 分'
    assert '甲 0' in lines[2]
    assert '推荐：' not in updates['说明'] and 'record_id' not in updates['说明']


def test_platform_display_hides_source_ids(platform_client):
    for platform,heading in [('平台甲', '对应人员'), ('平台乙', '轮流')]:
        updates, _ = run(platform_client, '找对应人员', 'creative', {'title': 'XX12345', 'platform': platform})
        lines = updates['说明'].split('\n\n')
        assert len(lines) == 4 and lines[0] == heading
        assert '商品归属记录' not in updates['说明']
        assert lines[2] == ('甲 0' if heading == '对应人员' else '乙 0')


def test_missing_score_display(client):
    updates, _ = recommend('self', {}, {}, None, M, client)
    assert updates['说明'].split('\n\n') == ['人工分配', '未准确匹配规则，暂时无法推荐', '人员负载未比较', '本任务分数待确定']
    client.data['tasks'] = [row('missing', 推荐人=[{'id': 'a'}])]
    assert run(client)[0]['说明'].split('\n\n') == [
        '轮流', '全体美工负载最低优先，同分轮流',
        '甲 0（1 条待计分） ｜ 乙 0', '本任务 4 分']


def test_sequential_recommendations_reserve_load_and_rotate(client):
    """连续需求回写后累计预占，并在同负载时推进轮换。"""
    winners = []
    for index in range(4):
        updates, pending = run(client, fields={'标题': '【历史验证】任务'})
        winner = updates['推荐人'][0]['id']
        winners.append(winner)
        client.data['tasks'].append(row(str(index), 推荐人=updates['推荐人'], 分数=4))
        save_rotation(pending)
    assert winners == ['a', 'b', 'a', 'b']
    assert workloads(client.data['tasks'], M, ['a', 'b'], 'self') == {
        'a': {'score': 8, 'count': 2, 'missing': []},
        'b': {'score': 8, 'count': 2, 'missing': []},
    }


@pytest.mark.parametrize('backups,expected', [([], ['a']), (['乙', '甲', '乙'], ['a', 'b']), (['未配置'], [])])
def test_shop_team_order_dedup_and_unknown_member(client, backups, expected):
    path = client.settings.fields_path.parent / 'recommendation.json'
    config = json.loads(path.read_text())
    config['shops'][0]['backup'] = backups
    path.write_text(json.dumps(config))
    updates, pending = run(client, '找对应人员', 'shop', {'department': '部门甲', 'platform': '店铺一'})
    assert updates['推荐人'] == [{'id': uid} for uid in expected]
    assert pending is None


def test_shop_multiple_deputies_follow_configuration_order(client):
    path = client.settings.fields_path.parent / 'recommendation.json'
    config = json.loads(path.read_text())
    config['members']['丙'] = 'c'
    config['rotation'].append('丙')
    config['shops'][0]['backup'] = ['丙', '乙']
    path.write_text(json.dumps(config))
    client.data['members'].append(row('c', 人员=[{'id': 'c'}]))
    updates, _ = run(client, '找对应人员', 'shop', {'department': '部门甲', 'platform': '店铺一'})
    assert updates['推荐人'] == [{'id': 'a'}, {'id': 'c'}, {'id': 'b'}]
    assert updates['说明'].split('\n\n')[2] == '甲 0 ｜ 丙 0 ｜ 乙 0'


def test_team_reservation_then_final_assignee_and_cancellation(client):
    updates, _ = run(client, '找对应人员', 'shop', {'department': '部门甲', 'platform': '店铺一'})
    record = row('team', 推荐人=updates['推荐人'], 分数=4)
    loads = workloads([record], M, ['a', 'b'], 'next')
    assert [loads[uid]['score'] for uid in ('a', 'b')] == [4, 4]
    assert [loads[uid]['count'] for uid in ('a', 'b')] == [1, 1]
    record['fields'].update(确认=True, 负责人=[{'id': 'b'}])
    loads = workloads([record], M, ['a', 'b'], 'next')
    assert [loads[uid]['score'] for uid in ('a', 'b')] == [0, 4]
    record['fields']['取消'] = True
    loads = workloads([record], M, ['a', 'b'], 'next')
    assert [loads[uid]['score'] for uid in ('a', 'b')] == [0, 0]


@pytest.mark.parametrize('owner,backups,expected', [
    ('乙', ['甲'], ['b', 'a']),
    ('甲', ['乙', '甲', '乙'], ['a', 'b']),
    ('甲', [], ['a']),
])
def test_shop_team_order_and_dedup(client, owner, backups, expected):
    """主副顺序来自配置，重复人员不产生重复推荐。"""
    path = client.settings.fields_path.parent / 'recommendation.json'
    config = json.loads(path.read_text())
    config['shops'][0].update(primary=owner, backup=backups)
    path.write_text(json.dumps(config))
    updates, state = run(client, '找对应人员', 'shop', {'department': '部门甲', 'title': '店铺一'})
    assert updates['推荐人'] == [{'id': uid} for uid in expected]
    assert state is None
    loads = workloads([row('new', 推荐人=updates['推荐人'], 分数=4)], M, ['a', 'b'], 'self')
    assert all(loads[uid]['score'] == 4 for uid in expected)
