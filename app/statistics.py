"""自动维护需求汇总与人员执行统计，原始父子记录由人工填写。"""

import fcntl
import hashlib
import json
import logging
import threading

from app.config import load_fields
from app.feishu import FeishuClient
from app.rules import load_score, plain
from app.task_records import checked, people, task_snapshot

logger = logging.getLogger(__name__)
PERSON_KEYS = {'assignee', 'recommended_assignee'}
CHECKBOX_KEYS = {'leader_confirmed', 'assignee_confirmed', 'cancelled'}


def statistics_config(settings):
    """读取容器外的统计字段映射与刷新间隔。"""
    config = json.loads((settings.fields_path.parent / 'statistics.json').read_text(encoding='utf-8'))
    if not isinstance(config.get('fields'), dict) or not 10 <= config.get('interval_seconds', 0) <= 3600:
        raise ValueError('统计字段映射或刷新间隔无效')
    if len(set(config['fields'].values())) != len(config['fields']):
        raise ValueError('统计字段映射重复')
    return config


def statistics_rows(records, mapping, config):
    """功能说明：构建供看板和成员负荷表使用的统计字段。

    参数 records：完整原始需求；mapping：原需求字段映射；config：统计表字段和范围名称配置。
    返回值：以需求或需求加人员作为稳定键的统计字段字典。
    """
    summaries, executions = task_snapshot(records, mapping)
    result = {}
    for kind, items in (('summary', summaries), ('person', executions)):
        for row in items:
            rid = row['record_id']
            key = f'{kind}:{rid}' + (f':{row["person_id"]}' if kind == 'person' else '')
            fields = row['fields']
            values = {'key': key, 'kind': config['kinds'][kind], 'source': rid}
            for name in config['fields']:
                if name in values:
                    continue
                value = fields.get(mapping.get(name))
                if name in PERSON_KEYS:
                    value = [{'id': uid} for uid in people(value)]
                elif name in CHECKBOX_KEYS:
                    value = checked(value)
                elif name == 'load_score':
                    value = load_score(value)
                elif name not in {'deadline', 'completed_at'}:
                    value = plain(value) or None
                values[name] = value
            result[key] = {config['fields'][name]: value for name, value in values.items()}
    return result


def comparable(fields, config):
    """统一人员名称、复选框空值和数字字符串，避免重复回写。"""
    result = {}
    for name, field in config['fields'].items():
        value = fields.get(field)
        if name in PERSON_KEYS:
            value = people(value)
        elif name in CHECKBOX_KEYS:
            value = checked(value)
        elif name == 'load_score':
            value = load_score(value)
        elif name not in {'deadline', 'completed_at'}:
            value = plain(value) or None
        result[field] = value
    return result


def sync_statistics(feishu):
    """功能说明：加锁后增量同步统计表，并清理来源已删除的统计行。

    参数 feishu：含统计表 ID 的业务客户端。返回值：创建、更新和删除行数；未配置时均为零。
    """
    settings = feishu.settings
    if not settings.statistics_table_id:
        return {'created': 0, 'updated': 0, 'deleted': 0}
    if settings.statistics_table_id == settings.table_id:
        raise ValueError('统计表必须独立于需求表')
    config = statistics_config(settings)
    mapping = load_fields(settings.fields_path)
    folder = settings.rules_cache_path.parent
    folder.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256((settings.base_token + settings.statistics_table_id).encode()).hexdigest()[:20]
    with (folder / f'statistics-{identity}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = feishu.get_records(settings.table_id)
        desired = statistics_rows(records, mapping, config)
        existing = feishu.get_records(settings.statistics_table_id)
        key_field = config['fields']['key']
        known, deleted = {}, []
        for row in existing:
            key = plain(row['fields'].get(key_field))
            # 只维护程序使用的键，人工添加的其他记录保留。
            if not key.startswith(('summary:', 'person:')):
                continue
            if key in known or key not in desired:
                deleted.append(row['record_id'])
            else:
                known[key] = row
        created = [{'fields': fields} for key, fields in desired.items() if key not in known]
        updated = [{'record_id': known[key]['record_id'], 'fields': fields} for key, fields in desired.items()
                   if key in known and comparable(fields, config) != comparable(known[key]['fields'], config)]
        # 写入全部完成后再删除旧行，异常时下一轮依据远端真实状态恢复。
        if created:
            feishu.mutate_statistics('batch_create', created)
        if updated:
            feishu.mutate_statistics('batch_update', updated)
        if deleted:
            feishu.mutate_statistics('batch_delete', deleted)
        counts = {'created': len(created), 'updated': len(updated), 'deleted': len(deleted)}
        if any(counts.values()):
            logger.info('统计同步完成 需求=%d 人员执行=%d 新增=%d 更新=%d 删除=%d',
                        sum(key.startswith('summary:') for key in desired),
                        sum(key.startswith('person:') for key in desired), *counts.values())
        return counts


class StatisticsSynchronizer:
    """独立刷新线程，人工编辑、删除原需求后自动更新统计。"""

    def __init__(self, settings):
        """初始化同步器，settings 提供凭证、表 ID 和配置路径。"""
        self.feishu = FeishuClient(settings)
        self.interval = statistics_config(settings)['interval_seconds']
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='statistics', daemon=True)

    def run(self):
        """功能说明：定期增量刷新，失败记录异常并在下一轮恢复。参数：无。返回值：无。"""
        logger.info('统计同步已启动 间隔=%ds', self.interval)
        while not self.stop.is_set():
            try:
                sync_statistics(self.feishu)
            except Exception:
                logger.exception('统计同步失败，将在下一轮重试')
            self.stop.wait(self.interval)

    def close(self):
        """停止定时刷新，等待本轮同步结束。"""
        self.stop.set()
        self.thread.join(timeout=5)
