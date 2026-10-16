"""父子需求的身份识别与按人员展开的统计快照。"""

from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


def parent_ids(fields, mapping):
    """读取关联的真实记录 ID；空关联的展示包装不代表存在父记录。"""
    value = fields.get(mapping.get('parent_record'))
    values = value if isinstance(value, list) else [value]
    ids = []
    for item in values:
        if isinstance(item, str) and item:
            ids.append(item)
        elif isinstance(item, dict):
            ids.extend(item.get('record_ids') or [])
            if item.get('id'):
                ids.append(item['id'])
    return list(dict.fromkeys(ids))


def people(value):
    """按原顺序去重人员 ID。"""
    return list(dict.fromkeys(item['id'] for item in value or []
                              if isinstance(item, dict) and item.get('id')))


def checked(value):
    """识别明确勾选的复选框。"""
    return value is True or value == 1


def task_snapshot(records, mapping, exclude_id=None):
    """功能说明：按主需求和人员生成去重快照，不写入原记录。

    参数 records：完整需求记录列表；mapping：含可选父记录的字段映射；
    exclude_id：试算时排除的记录 ID，其所在的整组父子需求一并排除。
    返回值：需求汇总列表与人员执行列表，均保留主需求 ID 和字段字典。
    """
    by_id = {row['record_id']: row for row in records}
    parents = {rid: parent_ids(row['fields'], mapping) for rid, row in by_id.items()}

    def root_of(rid):
        """定位根需求，异常关联不参与独立计分。"""
        seen = set()
        while rid in by_id and rid not in seen and len(seen) < 32:
            seen.add(rid)
            links = parents[rid]
            if not links:
                return rid
            if len(links) != 1:
                break
            rid = links[0]
        return None

    roots = {rid: root_of(rid) for rid in by_id}
    excluded = roots.get(exclude_id, exclude_id)
    descendants = defaultdict(list)
    for rid, root in roots.items():
        if root is None:
            logger.warning('子记录关联无效，保留主需求负载 record=%s', rid)
        elif rid != root:
            descendants[root].append(by_id[rid])
    summaries, executions = [], []
    for rid, row in by_id.items():
        if parents[rid] or rid == excluded:
            continue
        base = row['fields']
        assignees = people(base.get(mapping['assignee']))
        recommended = people(base.get(mapping['recommended_assignee']))
        members = list(dict.fromkeys(assignees + recommended))
        children = defaultdict(list)
        for child in descendants[rid]:
            owners = people(child['fields'].get(mapping['assignee']))
            if not owners:
                continue
            if len(owners) != 1 or owners[0] not in assignees:
                logger.warning('子记录负责人不唯一或不在主需求中，保留主需求负载 record=%s', child['record_id'])
                continue
            children[owners[0]].append(child)
        current = []
        for uid in members:
            fields = dict(base)
            fields[mapping['assignee']] = [{'id': uid}] if uid in assignees else []
            fields[mapping['recommended_assignee']] = [{'id': uid}] if uid in recommended else []
            child_rows = children[uid]
            child_id = None
            if len(child_rows) == 1:
                child = child_rows[0]
                child_id = child['record_id']
                # 分数、评级、需求内容及分配确认归属主需求；个人进度归属子记录。
                fields[mapping['completed_at']] = child['fields'].get(mapping['completed_at'])
                fields[mapping['cancelled']] = checked(base.get(mapping['cancelled'])) or checked(child['fields'].get(mapping['cancelled']))
                for key in ('assignee_confirmed', 'deadline'):
                    name = mapping.get(key)
                    if name and child['fields'].get(name) is not None:
                        fields[name] = child['fields'][name]
            elif len(child_rows) > 1:
                logger.warning('同一人员存在重复子记录，沿用主需求进度 root=%s person=%s', rid, uid)
            item = {'record_id': rid, 'person_id': uid, 'child_id': child_id, 'fields': fields}
            executions.append(item)
            if uid in assignees:
                current.append(item)
        summary = dict(base)
        if any(item['child_id'] for item in current):
            active = [item for item in current if not checked(item['fields'].get(mapping['cancelled']))]
            times = [item['fields'].get(mapping['completed_at']) for item in active]
            summary[mapping['completed_at']] = max(times) if times and all(times) else None
        summaries.append({'record_id': rid, 'fields': summary})
    return summaries, executions
