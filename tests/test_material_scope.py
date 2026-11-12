"""运营记录批次筛选、关联持久化与重跑隔离测试。"""

import json
from unittest.mock import Mock

import pytest

from app.config import ROOT, Settings
from app.feishu import FeishuClient
from app.material_scope import choose_rows, scope_links

CONFIG = json.loads((ROOT / 'config/material_scope.json').read_text())['sources'][0]


def row(identity, **values):
    """构造有商品标识和批次信息的运营记录。"""
    return {'record_id': identity, 'fields': {'sku': '123456', '款号': 'AB123', '品名': '示例商品',
            '创意图要求': '修改卖点', '提交时间': 1000, **values}}


def test_choose_only_pending_product_and_all_skus():
    records = [row('recOne'), row('recTwo', sku='234567'), row('recAssigned', 负责人='成员'),
               row('recSubmitted', 是否提交需求=True), row('recOther', 品名='其他商品')]
    assert [r['record_id'] for r in choose_rows(records, '示例商品创意图', CONFIG)] == ['recOne', 'recTwo']
    assert [r['record_id'] for r in choose_rows(records, 'SKU 234567 修改', CONFIG)] == ['recTwo']


@pytest.mark.parametrize('records,title', [
    ([row('recOne'), row('recTwo', 提交时间=2000)], '示例商品'),
    ([row('recOne'), row('recTwo', 创意图要求='另一批要求')], '示例商品'),
    ([row('recOne', 提交时间=None)], '示例商品'),
    ([row('recOne')], 'AB1234'),
    ([row('recOne', 负责人='成员')], '示例商品'),
])
def test_ambiguous_or_ineligible_rows_require_confirmation(records, title):
    with pytest.raises(ValueError):
        choose_rows(records, title, CONFIG)


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings('app', 'secret', 'base', 'table', 'token', tmp_path / 'fields.json',
                        rules_cache_path=tmp_path / 'rules.json', materials_path=ROOT / 'config/materials.json')
    (tmp_path / 'material_scope.json').write_text(json.dumps({'sources': [CONFIG]}))
    monkeypatch.setenv(CONFIG['base_token_env'], 'source')
    monkeypatch.setenv(CONFIG['table_id_env'], 'tblSource')
    monkeypatch.setenv(CONFIG['mirror_table_env'], 'tblMirror')
    feishu = Mock(spec=FeishuClient)
    feishu.settings = settings
    schema = [{'field_name': name} for name in row('recOne')['fields']] + [
        {'field_name': '负责人'}, {'field_name': '是否提交需求'}]
    feishu.material_list.side_effect = lambda path, *args: (schema if path.endswith('/fields') else [row('recOne')], False)
    feishu.get_records.return_value = [row('recMirror')]
    return feishu


def test_bind_then_reuse_after_status_and_batch_changes(client):
    fields = {'标题': '示例商品', '资料': 'https://kocotree.feishu.cn/base/source?table=tblSource'}
    mapping = {'title': '标题', 'description': '说明', 'document_link': '资料'}
    expected = ['https://kocotree.feishu.cn/base/source?table=tblSource&record=recOne']
    assert scope_links('recTask', fields, mapping, client) == expected
    client.update_record.assert_called_once_with('recTask', {'京东创意图': [{'record_ids': ['recMirror'], 'text': '示例商品'}]})
    client.material_list.reset_mock()
    assert scope_links('recTask', fields, mapping, client) == expected
    client.material_list.assert_not_called()
    client.update_record.assert_called_once()


def test_existing_links_work_after_assignment_and_manual_change_invalidates_cache(client):
    client.material_list.side_effect = lambda path, *args: ([{'field_name': k} for k in
        [*row('recOne')['fields'], '负责人', '是否提交需求']] if path.endswith('/fields') else
        [row('recOne', 负责人='成员', 是否提交需求=True)], False)
    fields = {'资料': 'https://kocotree.feishu.cn/base/source?table=tblSource', '京东创意图': [{'record_ids': ['recMirror'], 'text': '示例商品'}]}
    assert 'record=recOne' in scope_links('recTask', fields, {'document_link': '资料'}, client)[0]
    client.update_record.assert_not_called()
    fields['京东创意图'] = ['recMissing']
    with pytest.raises(ValueError, match='不存在'):
        scope_links('recTask', fields, {'document_link': '资料'}, client)


def test_missing_mirror_never_writes_or_binds(client):
    client.get_records.return_value = []
    with pytest.raises(ValueError, match='尚未同步'):
        scope_links('recTask', {'标题': '示例商品', '资料': 'https://kocotree.feishu.cn/base/source?table=tblSource'},
                    {'title': '标题', 'document_link': '资料'}, client)
    client.update_record.assert_not_called()
    assert not (client.settings.rules_cache_path.parent / 'material_scopes').exists()


def test_explicit_record_and_unconfigured_documents_keep_scope(client):
    value = 'https://kocotree.feishu.cn/base/source?table=tblSource&record=recExplicit'
    assert scope_links('recTask', {'资料': value}, {'document_link': '资料'}, client) == [value]
    client.material_list.assert_not_called()
