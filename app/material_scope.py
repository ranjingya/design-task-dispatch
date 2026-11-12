"""确定运营多维表的本次需求记录，持久化范围供重跑复用。"""

import hashlib
import json
import logging
import os
import re
from urllib.parse import quote

from app.online_materials import document_links, parse_link
from app.rules import plain

logger = logging.getLogger(__name__)


def choose_rows(rows, demand, config):
    """功能说明：按商品标识和未提交状态选择唯一批次。参数 rows：运营记录；demand：需求文字；config：字段配置。返回值：本次记录列表，无法唯一对应时抛错。"""
    pending = [r for r in rows if not plain(r['fields'].get(config['owner_field']))
               and r['fields'].get(config['submitted_field']) in (None, False, 0)]
    selected = []
    for name in config['identity_fields']:
        selected = [r for r in pending if len(plain(r['fields'].get(name))) >= 3
                    and re.search(r'(?<![A-Za-z0-9])' + re.escape(plain(r['fields'][name])) + r'(?![A-Za-z0-9])', demand, re.I)]
        if selected:
            break
    if not selected:
        raise ValueError('未找到商品对应且负责人为空、尚未提交的运营记录，请明确关联记录')
    batches = {tuple(plain(r['fields'].get(k)) if not isinstance(r['fields'].get(k), (int, float))
                     else str(r['fields'][k]) for k in config['batch_fields']) for r in selected}
    if len(batches) != 1 or any(not value for batch in batches for value in batch):
        raise ValueError('同款运营记录存在多个批次或缺少批次信息，请明确关联记录')
    return selected


def scope_links(record_id, fields, mapping, feishu):
    """功能说明：为配置的运营表锁定记录并维护需求关联。参数 record_id：需求ID；fields：当前需求字段；mapping：字段映射；feishu：业务客户端。返回值：限定到具体记录的资料链接；其他资料保持原范围。"""
    settings = feishu.settings
    value = fields.get(mapping.get('document_link'))
    path = settings.fields_path.parent / 'material_scope.json'
    if not value or not path.exists():
        return value
    config = json.loads(path.read_text())
    materials = json.loads(settings.materials_path.read_text())
    result = []
    for link in document_links(value):
        try:
            kind, base, selection = parse_link(link, materials)
        except ValueError:
            result.append(link)
            continue
        source = next((s for s in config['sources'] if kind == 'base'
                       and base == os.getenv(s['base_token_env'])
                       and selection.get('table') == os.getenv(s['table_id_env'])), None)
        if source is None or selection.get('record'):
            result.append(link)
            continue
        table = selection['table']
        key = hashlib.sha256(json.dumps([settings.base_token, settings.table_id, record_id, base, table]).encode()).hexdigest()
        folder = settings.rules_cache_path.parent / 'material_scopes'
        snapshot = folder / (key + '.json')
        refs = fields.get(source['link_field']) or []
        linked = []
        for ref in refs:
            linked.extend(ref.get('record_ids', [ref.get('id') or ref.get('record_id')])
                          if isinstance(ref, dict) else [ref])
        if any(not isinstance(rid, str) or not re.fullmatch(r'rec[A-Za-z0-9]+', rid) for rid in linked):
            raise ValueError('需求关联记录格式无效，请核对关联列')
        saved = json.loads(snapshot.read_text()) if snapshot.exists() else None
        if saved and linked == saved['linked_records']:
            ids = saved['records']
            if not ids or any(not re.fullmatch(r'rec[A-Za-z0-9]+', rid) for rid in ids):
                raise ValueError('运营记录范围缓存无效，请核对后重新关联')
            logger.info('运营记录对应 方式=复用记录 范围=%d', len(ids))
        else:
            root = f'/bitable/v1/apps/{quote(base, safe="")}/tables/{quote(table, safe="")}'
            schema, more = feishu.material_list(root + '/fields', materials['max_base_fields'])
            names = {f['field_name'] for f in schema}
            required = set(source['identity_fields'] + source['batch_fields'] + source['mirror_match_fields']
                           + [source['owner_field'], source['submitted_field']])
            if more or not required <= names:
                raise ValueError('运营表字段不完整，请核对对应配置')
            rows, more = feishu.material_list(root + '/records', materials['max_base_records'],
                                            {'view_id': selection['view']} if selection.get('view') else {})
            if more:
                raise ValueError('运营表记录范围过大，请提供具体记录或缩小视图')
            mirror = os.getenv(source['mirror_table_env'], '')
            if not mirror:
                raise ValueError('运营记录关联表参数未配置')
            copies = feishu.get_records(mirror)

            def signature(row):
                """以商品与制作要求核对同步表记录，不使用易变的负责人。"""
                return tuple(plain(row['fields'].get(k)) for k in source['mirror_match_fields'])

            if linked:
                by_id = {r['record_id']: r for r in copies}
                if any(rid not in by_id for rid in linked):
                    raise ValueError('需求关联的运营记录不存在，请核对关联列')
                selected = []
                for rid in linked:
                    matches = [r for r in rows if signature(r) == signature(by_id[rid])]
                    if len(matches) != 1:
                        raise ValueError('已关联记录在运营源表中不能唯一对应，请提供具体记录链接')
                    selected.extend(matches)
            else:
                text = ' '.join(plain(fields.get(mapping.get(k))) for k in ('title', 'description'))
                selected = choose_rows(rows, text, source)
                linked = []
                for row in selected:
                    matches = [r for r in copies if signature(r) == signature(row)]
                    if len(matches) != 1:
                        raise ValueError('本批运营记录尚未同步或存在重复，请核对关联表后重试')
                    linked.append(matches[0]['record_id'])
                feishu.update_record(record_id, {source['link_field']: linked})
                fields[source['link_field']] = linked
            ids = list(dict.fromkeys(r['record_id'] for r in selected))
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = snapshot.with_suffix('.tmp')
            temporary.write_text(json.dumps({'records': ids, 'linked_records': linked}))
            temporary.chmod(0o600)
            temporary.replace(snapshot)
            logger.info('运营记录对应 方式=%s 范围=%d', '已有明确关联' if refs else '待提交批次', len(ids))
        # 多条具体记录链接共用原有材料限额，避免整表图片进入模型。
        prefix = link.split('?', 1)[0]
        result.extend(f'{prefix}?table={table}&record={rid}' for rid in ids)
    return result
