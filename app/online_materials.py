"""读取明确提交的飞书在线资料及其内嵌内容。"""

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from app.documents import read_document


def document_links(value):
    """解析超链接、富文本或多行链接输入，不主动追踪正文中的普通链接。"""
    if isinstance(value, list):
        return [link for item in value for link in document_links(item)]
    if isinstance(value, dict):
        return document_links(value.get('link') or value.get('url') or value.get('text') or '')
    if not isinstance(value, str):
        raise ValueError('需求文档链接格式无效')
    links = re.findall(r'https?://[^\s<>"，；]+', value)
    return list(dict.fromkeys(link.rstrip(')）。,;；') for link in links)) if links else ([value.strip()] if value.strip() else [])


def parse_link(value, config):
    """校验允许的飞书网址，保留表、视图、记录定位参数。"""
    url = urlsplit(value.strip())
    hosts = config.get('allowed_document_hosts', config['allowed_sheet_hosts'])
    if url.scheme != 'https' or url.hostname not in hosts or url.username or url.password or url.port not in (None, 443):
        raise ValueError('仅支持配置允许域名下的 HTTPS 飞书资料链接，不支持外部或网盘链接')
    match = re.fullmatch(r'/(docx|wiki|sheets|base)/([A-Za-z0-9]+)/?', url.path)
    if not match:
        raise ValueError('暂不支持此飞书链接类型；支持 /docx/、/wiki/、/sheets/、/base/')
    query = parse_qs(url.query)
    for name, prefix in (('table', 'tbl'), ('view', 'vew'), ('record', 'rec')):
        if name in query and (len(query[name]) != 1 or not re.fullmatch(prefix + r'[A-Za-z0-9]+', query[name][0])):
            raise ValueError(f'链接的 {name} 定位参数无效或不是数据表资源')
    return match[1], match[2], {key: query[key][0] for key in ('table', 'view', 'record') if key in query}


def block_text(block):
    """提取文本块的可读内容，普通引用只保留说明，不跟随跳转。"""
    pieces = []
    for value in block.values():
        if not isinstance(value, dict):
            continue
        for element in value.get('elements', []):
            if 'text_run' in element:
                pieces.append(element['text_run'].get('content', ''))
            elif 'equation' in element:
                pieces.append(element['equation'].get('content', ''))
            elif 'mention_doc' in element:
                pieces.append(element['mention_doc'].get('title', '引用文档'))
            elif 'mention_user' in element:
                pieces.append(element['mention_user'].get('name', '提及人员'))
            elif 'link_preview' in element:
                pieces.append(element['link_preview'].get('title', '链接'))
            elif 'reminder' in element:
                pieces.append(str(element['reminder'].get('expire_time', '提醒')))
    return ''.join(pieces)


class OnlineReader:
    """共享材料限额，去重并限制在线内嵌资源的递归深度。"""

    def __init__(self, feishu, bundle):
        self.feishu, self.bundle = feishu, bundle
        self.config = bundle.config
        self.seen = set()

    def read_link(self, link, source):
        """功能说明：读取提交的飞书链接。参数 link：链接；source：来源名称。返回值：无。"""
        kind, token, selection = parse_link(link, self.config)
        self.read(kind, token, selection, source, 0)

    def read(self, kind, token, selection, source, depth):
        """功能说明：递归读取明确内嵌的资源。参数 kind：资源类型；token：令牌；selection：表、视图或记录范围；source：来源；depth：内嵌深度。返回值：无。"""
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9]+', token):
            raise ValueError('内嵌资料令牌无效')
        key = (kind, token, tuple(sorted(selection.items())))
        if key in self.seen:
            return
        if depth > self.config['max_embed_depth']:
            raise ValueError('内嵌资料层级超过上限')
        if len(self.seen) >= self.config['max_online_resources']:
            raise ValueError('在线资料数量超过上限')
        self.seen.add(key)
        if kind == 'wiki':
            node = self.feishu.material_get('/wiki/v2/spaces/get_node', {'token': token})['node']
            target = {'docx': 'docx', 'sheet': 'sheets', 'bitable': 'base'}.get(node.get('obj_type'))
            if not target:
                raise ValueError(f"知识库中的资料类型暂不支持：{node.get('obj_type')}")
            self.read(target, node['obj_token'], selection, source, depth + 1)
        elif kind == 'sheets':
            self.bundle.claim_file()
            data = self.feishu.export_sheet(token, self.bundle.file_limit(), self.config['export_attempts'], self.config['export_interval_seconds'])
            self.bundle.file_size += len(data)
            read_document(data, '.xlsx', source, self.bundle)
        elif kind == 'docx':
            self.read_docx(token, source, depth)
        elif kind == 'base':
            self.read_base(token, selection, source)
        else:
            raise ValueError('暂不支持此在线资料类型')

    def embedded(self, kind, token, source, depth):
        """解析文档块中的资源定位，内嵌资源失败时保留其余正文。"""
        try:
            parts = token.split('_')
            selection = {}
            if kind == 'base' and len(parts) > 1:
                selection['table'] = parts[1]
                if len(parts) > 2:
                    selection['view'] = parts[2]
            self.read(kind, parts[0], selection, source, depth + 1)
        except (ValueError, RuntimeError) as exc:
            self.bundle.issue(source, str(exc))
        except Exception:
            self.bundle.issue(source, '内嵌资料无法完整解析')

    def read_docx(self, token, source, depth):
        """功能说明：按文档层级读取正文、表格、图片和内嵌资源。参数 token：文档令牌；source：来源；depth：当前层级。返回值：无。"""
        blocks, truncated = self.feishu.material_list(f'/docx/v1/documents/{quote(token, safe="")}/blocks', self.config['max_doc_blocks'])
        if truncated:
            self.bundle.issue(source, '文档块数量超过上限，未读完全部内容')
        by_id = {block['block_id']: block for block in blocks}
        visited = set()
        structural = {1, 18, 19, 22, 23, 24, 25, 31, 32, 34, 44}

        def visit(block, label, ancestors):
            """按子块顺序还原段落及表格单元格，并检测截断结构。"""
            if len(ancestors) > 64:
                self.bundle.issue(label, '文档层级超过上限')
                return
            identity = block['block_id']
            if identity in visited:
                return
            visited.add(identity)
            text = block_text(block)
            context = '\n'.join([*ancestors[-2:], text]).strip()
            if text:
                self.bundle.add_text(label, text)
            if 'image' in block:
                self.media(block['image'], label, context, image=True)
            elif 'board' in block:
                try:
                    if self.bundle.image_available(label):
                        data = self.feishu.download_board(block['board']['token'], self.bundle.file_limit())
                        self.bundle.file_size += len(data)
                        self.bundle.add_image(data, label + ' / 画板', group=source, context=context, requires_detail=True)
                except (ValueError, RuntimeError) as exc:
                    self.bundle.issue(label, str(exc))
                except Exception:
                    self.bundle.issue(label, '画板预览无法完整解析')
            elif 'add_ons' in block and self.is_notes_component(block['add_ons']):
                self.bundle.add_text(label, '智能纪要交互组件不参与读取；正文独立读取，音视频不读取')
            elif 'file' in block:
                self.media(block['file'], label, context)
            elif 'sheet' in block:
                self.embedded('sheets', block['sheet']['token'], label + ' / 内嵌电子表格', depth)
            elif 'bitable' in block:
                self.embedded('base', block['bitable']['token'], label + ' / 内嵌多维表', depth)
            elif not text and block.get('block_type') not in structural:
                # 空段落不会影响材料完整性，未解析的资源块必须显式说明。
                if not any(isinstance(value, dict) and 'elements' in value for value in block.values()):
                    self.bundle.issue(label, f"存在暂不能读取的文档内容块（类型 {block.get('block_type')}）")
            previous = context
            for index, child in enumerate(block.get('children', []), 1):
                if child not in by_id:
                    self.bundle.issue(label, '部分子内容块未返回，无法确认完整材料')
                    continue
                columns = max(1, block.get('table', {}).get('property', {}).get('column_size', 1))
                child_label = (f'{label} / 第{(index - 1) // columns + 1}行第{(index - 1) % columns + 1}列'
                               if 'table' in block else f'{label} / {index}')
                visit(by_id[child], child_label, [*ancestors, previous])
                previous = block_text(by_id[child]) or previous
        for block in blocks:
            if block.get('parent_id') not in by_id:
                visit(block, source, [])
        if len(visited) != len(blocks):
            self.bundle.issue(source, '文档块层级异常，存在未读取内容')

    @staticmethod
    def is_notes_component(component):
        """识别纪要播放器与反馈等交互组件，正文仍由独立文本块读取。"""
        try:
            record = json.loads(component.get('record', '{}'))
            return isinstance(record, dict) and record.get('bizType') == 'ai_notes'
        except (ValueError, TypeError):
            return False

    def media(self, item, source, context='', image=False, **location):
        """限量读取在线资源的图片或附件，共用附件解析策略。"""
        try:
            name = item.get('name', '')
            if not image and Path(name).suffix.lower() in ('.zip', '.rar', '.7z'):
                self.bundle.add_text(source, '素材压缩包不参与识别')
                return
            if image and not self.bundle.image_available(source):
                return
            if not image:
                self.bundle.claim_file()
                if Path(name).suffix.lower() not in ('.doc', '.docx', '.xlsx', '.xls', '.pdf', '.png', '.jpg', '.jpeg', '.webp'):
                    raise ValueError(f'内嵌附件类型暂不支持：{name or "未提供文件名"}')
            data = self.feishu.download_material_media(item.get('token') or item.get('file_token'), self.bundle.file_limit(), **location)
            self.bundle.file_size += len(data)
            if image or Path(name).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'):
                keywords = self.config['visual_review']['instruction_keywords'] + self.config['visual_review']['image_reference_keywords']
                self.bundle.add_image(data, source, group=source.split(' / ')[0], context=context,
                                      requires_detail=not context or any(word in context for word in keywords))
            else:
                read_document(data, Path(name).suffix.lower(), source + ' / ' + name, self.bundle)
        except (ValueError, RuntimeError) as exc:
            self.bundle.issue(source, str(exc))
        except Exception:
            self.bundle.issue(source, '在线图片或附件无法完整解析')

    def read_base(self, token, selection, source):
        """功能说明：读取指定多维表范围及其附件。参数 token：Base令牌；selection：数据表、视图、记录定位；source：来源。返回值：无。"""
        root = f'/bitable/v1/apps/{quote(token, safe="")}/tables'
        table = selection.get('table')
        if not table:
            tables, more = self.feishu.material_list(root, 2)
            if len(tables) != 1 or more:
                raise ValueError('多维表包含多个数据表，请提供带 table 参数的具体数据表链接')
            table = tables[0]['table_id']
        if not re.fullmatch(r'tbl[A-Za-z0-9]+', table):
            raise ValueError('多维表链接没有有效数据表定位')
        path = root + '/' + quote(table, safe='')
        fields, more = self.feishu.material_list(path + '/fields', self.config['max_base_fields'])
        if more:
            self.bundle.issue(source, '多维表字段数量超过上限')
        if selection.get('record'):
            rows = [self.feishu.material_get(path + '/records/' + quote(selection['record'], safe=''))['record']]
        else:
            query = {'view_id': selection['view']} if selection.get('view') else {}
            rows, more = self.feishu.material_list(path + '/records', self.config['max_base_records'], query)
            if more:
                self.bundle.issue(source, '多维表记录超过上限，请提供具体记录链接或缩小视图范围')
        for row in rows:
            label = f"{source} / 记录 {row['record_id']}"
            values = row.get('fields', {})
            for field in fields:
                name = field['field_name']
                value = values.get(name)
                if value is None:
                    continue
                if field['type'] == 17:
                    for index, item in enumerate(value, 1):
                        self.media(item, f'{label} / {name} {index}', str(values.get(fields[0]['field_name'], '')),
                                   image=str(item.get('type', '')).startswith('image/'), table_id=table,
                                   record_id=row['record_id'], field_id=field['field_id'])
                else:
                    # 保留显示文本与字段名称；关联记录和普通链接不继续递归读取。
                    self.bundle.add_text(label, f'{name}：{json.dumps(value, ensure_ascii=False)}')
