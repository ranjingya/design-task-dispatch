"""人员推荐、归属匹配、负载预占及人工保护测试。"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import ROOT
from app.recommendation import workloads, product_owner, shop_owner, recommend, save_rotation, protected

M = {'recommended_assignee': '推荐人', 'assignee': '负责人', 'leader_confirmed': '确认',
     'completed_at': '完成', 'cancelled': '取消', 'is_test': '测试', 'load_score': '分数',
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
               row('test', 推荐人=[{'id': 'a'}], 测试=True, 分数=99),
               row('cancel', 推荐人=[{'id': 'a'}], 取消=True, 分数=99),
               row('complete', 负责人=[{'id': 'b'}], 确认=True, 完成=123, 分数=99),
               row('missing', 推荐人=[{'id': 'b'}])]
    result = workloads(records, M, ['a', 'b'], 'self')
    assert result['a'] == {'score': 4, 'count': 1, 'missing': []}
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
    (tmp_path / 'recommendation.json').write_text(json.dumps(config))
    f = Mock()
    f.settings = SimpleNamespace(fields_path=tmp_path / 'fields.json', rules_cache_path=tmp_path / 'cache/rules.json', table_id='tasks')
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
    assert run(client, fields={'测试': True})[1] is None


def test_minimum_load_fixed_pool_and_missing_score(client):
    client.data['tasks'] = [row('busy', 推荐人=[{'id': 'a'}], 分数=8)]
    assert run(client)[0]['推荐人'] == [{'id': 'b'}]
    assert run(client, '固定人员（甲）')[0]['推荐人'] == [{'id': 'a'}]
    client.data['tasks'].append(row('missing', 推荐人=[{'id': 'b'}]))
    assert run(client)[0]['推荐人'] == []
    assert '缺分' in run(client)[0]['说明']
    assert run(client, '固定人员（未知）')[0]['推荐人'] == []


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
    assert updates['推荐人'] == [{'id': 'a'}] and '乙' in updates['说明']


@pytest.mark.parametrize('fields', [{'负责人': [{'id': 'a'}]}, {'确认': True}, {'取消': True}, {'完成': 123}])
def test_protected_records(fields):
    assert protected(fields, M)


def test_configuration_routes_and_members_consistent():
    config = json.loads((ROOT / 'config/recommendation.json').read_text())
    assert len(config['shops']) == 10
    assert len(config['rotation']) == len(set(config['rotation'])) == len(config['members'])
    for shop in config['shops']:
        assert all(n in config['members'] for n in [shop['primary']] + shop['backup'])
