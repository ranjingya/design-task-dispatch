"""在线资料类型分流、范围控制与错误隔离测试。"""

import io
import json
from unittest.mock import Mock

import pytest
from PIL import Image

from app.config import ROOT, Settings
from app.feishu import FeishuClient
from app.materials import MaterialBundle, collect_materials
from app.online_materials import OnlineReader, document_links, parse_link


@pytest.fixture
def bundle(tmp_path):
    settings = Settings('app', 'secret', 'base', 'table', 'token', tmp_path / 'fields.json')
    return MaterialBundle(settings, json.loads((ROOT / 'config/materials.json').read_text()))


def text_block(identity, content, parent='doc', kind='text', children=None):
    """构造带层级和段落的官方文档块。"""
    return {'block_id': identity, 'parent_id': parent, 'block_type': 2,
            kind: {'elements': [{'text_run': {'content': content}}]}, 'children': children or []}


def picture():
    """生成小尺寸图片。"""
    out = io.BytesIO()
    Image.new('RGB', (40, 40), 'white').save(out, format='PNG')
    return out.getvalue()


def test_links_types_queries_and_rich_text(bundle):
    value = [{'link': 'https://kocotree.feishu.cn/docx/abc?dcuId=123'},
             {'text': '[材料](https://kocotree.feishu.cn/wiki/def)\nhttps://kocotree.feishu.cn/base/ghi?table=tbl123&view=vew456&record=rec789'}]
    links = document_links(value)
    assert len(links) == 3
    assert [parse_link(link, bundle.config)[0] for link in links] == ['docx', 'wiki', 'base']
    assert parse_link(links[2], bundle.config)[2] == {'table': 'tbl123', 'view': 'vew456', 'record': 'rec789'}


@pytest.mark.parametrize('url', ['http://kocotree.feishu.cn/docx/abc', 'https://kocotree.feishu.cn.evil.test/docx/abc',
    'https://user@kocotree.feishu.cn/docx/abc', 'https://kocotree.feishu.cn:8080/docx/abc',
    'https://kocotree.feishu.cn/slides/abc', 'https://kocotree.feishu.cn/base/abc?table=dashboard',
    'https://kocotree.feishu.cn/base/abc?table=tblA&table=tblB', 'https://pan.example.com/file'])
def test_reject_external_and_wrong_resource(bundle, url):
    with pytest.raises(ValueError):
        parse_link(url, bundle.config)


def test_docx_order_table_images_and_no_link_chasing(bundle):
    client = Mock(spec=FeishuClient)
    blocks = [{'block_id': 'doc', 'block_type': 1, 'children': ['intro', 'table', 'image']},
              text_block('intro', '修改要求：见图，https://external.example/'),
              {'block_id': 'table', 'block_type': 31, 'parent_id': 'doc', 'table': {'property': {'column_size': 2}}, 'children': ['cell1', 'cell2']},
              {'block_id': 'cell1', 'parent_id': 'table', 'block_type': 32, 'children': ['p1']},
              {'block_id': 'cell2', 'parent_id': 'table', 'block_type': 32, 'children': ['p2']},
              text_block('p2', '750×750', 'cell2'), text_block('p1', '尺寸要求', 'cell1'),
              {'block_id': 'image', 'block_type': 27, 'parent_id': 'doc', 'image': {'token': 'pic'}}]
    client.material_list.return_value = (blocks, False)
    client.download_material_media.return_value = picture()
    OnlineReader(client, bundle).read_link('https://kocotree.feishu.cn/docx/doc', '需求')
    assert not bundle.issues
    assert [row['text'] for row in bundle.texts] == ['修改要求：见图，https://external.example/', '尺寸要求', '750×750']
    assert '第1行第2列' in bundle.texts[2]['source']
    assert len(bundle.images) == 1 and bundle.image_sources[0]['requires_detail']
    assert client.material_list.call_count == 1


def test_wiki_resolves_docx_and_deduplicates(bundle):
    client = Mock(spec=FeishuClient)
    client.material_get.return_value = {'node': {'obj_type': 'docx', 'obj_token': 'document'}}
    client.material_list.return_value = ([text_block('a', '工作内容')], False)
    reader = OnlineReader(client, bundle)
    reader.read_link('https://kocotree.feishu.cn/wiki/node', '需求')
    reader.read_link('https://kocotree.feishu.cn/docx/document', '重复链接')
    assert client.material_list.call_count == 1
    assert bundle.texts[0]['text'] == '工作内容'


def test_embedded_sheet_and_base_follow_scope(bundle, monkeypatch):
    client = Mock(spec=FeishuClient)
    blocks = [{'block_id': 's', 'block_type': 30, 'sheet': {'token': 'spreadsheet_sheetA'}},
              {'block_id': 'b', 'block_type': 18, 'bitable': {'token': 'base_tblA_vewA'}}]
    client.material_list.side_effect = [(blocks, False), ([{'field_name': '任务', 'field_id': 'fldA', 'type': 1}], False),
                                        ([{'record_id': 'recA', 'fields': {'任务': '拍摄主图'}}], False)]
    client.export_sheet.return_value = b'xlsx'
    parser = Mock()
    monkeypatch.setattr('app.online_materials.read_document', parser)
    OnlineReader(client, bundle).read_link('https://kocotree.feishu.cn/docx/doc', '需求')
    assert client.export_sheet.call_args.args[0] == 'spreadsheet'
    assert client.material_list.call_args.args == ('/bitable/v1/apps/base/tables/tblA/records', 100, {'view_id': 'vewA'})
    assert '拍摄主图' in bundle.texts[0]['text']
    assert not bundle.issues


def test_base_record_does_not_load_other_records(bundle):
    client = Mock(spec=FeishuClient)
    client.material_list.return_value = ([{'field_name': '需求', 'field_id': 'fldA', 'type': 1}], False)
    client.material_get.return_value = {'record': {'record_id': 'recA', 'fields': {'需求': '单条需求'}}}
    OnlineReader(client, bundle).read_link('https://kocotree.feishu.cn/base/base?table=tblA&view=vewA&record=recA', '需求')
    assert client.material_get.call_args.args[0].endswith('/records/recA')
    assert client.material_list.call_count == 1


def test_base_multiple_tables_needs_selection(bundle):
    client = Mock(spec=FeishuClient)
    client.material_list.return_value = ([{'table_id': 'tblA'}, {'table_id': 'tblB'}], False)
    with pytest.raises(ValueError, match='多个数据表'):
        OnlineReader(client, bundle).read_link('https://kocotree.feishu.cn/base/base', '需求')


def test_base_attachment_uses_original_location(bundle):
    client = Mock(spec=FeishuClient)
    client.material_list.side_effect = [([{'field_name': '附件', 'field_id': 'fldFile', 'type': 17}], False),
        ([{'record_id': 'recA', 'fields': {'附件': [{'file_token': 'pic', 'type': 'image/png', 'name': '流程.png'}]}}], False)]
    client.download_material_media.return_value = picture()
    OnlineReader(client, bundle).read_link('https://kocotree.feishu.cn/base/base?table=tblA', '需求')
    assert client.download_material_media.call_args.kwargs == {'table_id': 'tblA', 'record_id': 'recA', 'field_id': 'fldFile'}
    assert len(bundle.images) == 1


def test_embedded_unreadable_attachments_keep_document_body(bundle):
    """在线文档中的未知附件和损坏图片不会阻止正文及其余图片识别。"""
    client = Mock(spec=FeishuClient)
    client.material_list.return_value = ([text_block('a', '制作商品主图'),
        {'block_id': 'file', 'block_type': 23, 'file': {'token': 'psd', 'name': '模板.psd'}},
        {'block_id': 'broken', 'block_type': 27, 'image': {'token': 'broken'}},
        {'block_id': 'valid', 'block_type': 27, 'image': {'token': 'valid'}}], False)
    client.download_material_media.side_effect = [RuntimeError('图片下载失败'), picture()]
    OnlineReader(client, bundle).read_link('https://kocotree.feishu.cn/docx/doc', '需求')
    assert [row['text'] for row in bundle.texts] == ['制作商品主图']
    assert len(bundle.images) == 1 and len(bundle.skipped_attachments) == 2
    assert not bundle.issues
    assert client.download_material_media.call_count == 2


def test_document_limit_and_unsupported_block_report_issues(bundle):
    client = Mock(spec=FeishuClient)
    client.material_list.return_value = ([text_block('a', '正文'), {'block_id': 'b', 'block_type': 999}], True)
    OnlineReader(client, bundle).read_link('https://kocotree.feishu.cn/docx/doc', '需求')
    assert len(bundle.issues) == 2
    assert bundle.texts[0]['text'] == '正文'


def test_one_failed_link_does_not_discard_next(bundle):
    client = Mock(spec=FeishuClient, settings=bundle.settings)
    client.material_list.return_value = ([text_block('a', '有效正文')], False)
    result = collect_materials([], 'https://evil.test/docx/a\nhttps://kocotree.feishu.cn/docx/b', 'recA', '', client)
    assert len(result.issues) == 1
    assert result.texts[0]['text'] == '有效正文'


def test_bounded_pagination_and_cursor_failure(bundle, monkeypatch):
    client = FeishuClient(bundle.settings)
    get = Mock(side_effect=[{'items': [1, 2], 'has_more': True, 'page_token': 'next'}, {'items': [3, 4], 'has_more': False}])
    monkeypatch.setattr(client, 'material_get', get)
    assert client.material_list('/test', 3) == ([1, 2, 3], True)
    get.side_effect = [{'items': [], 'has_more': True, 'page_token': 'same'}] * 2
    with pytest.raises(RuntimeError, match='分页异常'):
        client.material_list('/test', 3)


@pytest.mark.parametrize('message,expected', [('HTTP=403', '读取权限'), ('HTTP=404', '不存在'), ('HTTP=400 code=123', '并非链接格式错误')])
def test_permission_and_missing_are_not_invalid_urls(bundle, monkeypatch, message, expected):
    client = FeishuClient(bundle.settings)
    monkeypatch.setattr(client, '_access_token', lambda: 'token')
    monkeypatch.setattr(client, '_request', Mock(side_effect=RuntimeError(message)))
    with pytest.raises(RuntimeError, match=expected):
        client.material_get('/test')


def test_board_and_notes_controls_preserve_readable_body(bundle):
    client = Mock(spec=FeishuClient)
    client.material_list.return_value = ([text_block('a', '需求正文'),
        {'block_id': 'board', 'block_type': 43, 'board': {'token': 'boardtoken'}},
        {'block_id': 'control', 'block_type': 40, 'add_ons': {'record': '{"bizType":"ai_notes"}'}}], False)
    client.download_board.return_value = picture()
    OnlineReader(client, bundle).read_link('https://kocotree.feishu.cn/docx/doc', '需求')
    assert not bundle.issues
    assert bundle.texts[0]['text'] == '需求正文'
    assert len(bundle.images) == 1 and bundle.image_sources[0]['requires_detail']
    client.download_material_media.assert_not_called()


@pytest.mark.parametrize('failure', [RuntimeError('导出权限不足'), OSError('损坏的文档')])
def test_failed_embed_keeps_following_body(bundle, failure):
    client = Mock(spec=FeishuClient)
    client.material_list.return_value = ([{'block_id': 's', 'block_type': 30, 'sheet': {'token': 'sheet'}},
                                         text_block('a', '后续需求')], False)
    client.export_sheet.side_effect = failure
    OnlineReader(client, bundle).read_link('https://kocotree.feishu.cn/docx/doc', '需求')
    assert bundle.texts[0]['text'] == '后续需求'
    assert len(bundle.issues) == 1


def test_resource_depth_and_count_are_bounded(bundle):
    client = Mock(spec=FeishuClient)
    client.material_list.return_value = ([text_block('a', '需求')], False)
    reader = OnlineReader(client, bundle)
    with pytest.raises(ValueError, match='层级'):
        reader.read('docx', 'deep', {}, '需求', bundle.config['max_embed_depth'] + 1)
    bundle.config['max_online_resources'] = 1
    reader.read_link('https://kocotree.feishu.cn/docx/first', '需求')
    with pytest.raises(ValueError, match='数量'):
        reader.read_link('https://kocotree.feishu.cn/docx/second', '需求')
    assert client.material_list.call_count == 1
