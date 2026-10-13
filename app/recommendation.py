"""按规则、业务归属和实时负载生成可解释的人员推荐。"""

import json
import logging
import re
from pathlib import Path

from app.rules import load_score, plain

logger = logging.getLogger(__name__)


def checked(value):
    """识别飞书复选框的明确真值。"""
    return value is True or value == 1


def user_ids(value):
    """读取人员字段的真实用户 ID。"""
    return [v['id'] for v in value or [] if isinstance(v, dict) and v.get('id')]


def protected(fields, mapping):
    """人工已安排、已确认、完成或取消的记录保留原结果。"""
    return bool(fields.get(mapping.get('assignee')) or fields.get(mapping.get('completed_at')) or
                checked(fields.get(mapping.get('leader_confirmed'))) or
                checked(fields.get(mapping.get('cancelled'))))


def workloads(records, mapping, members, exclude_id):
    """功能说明：计算正式任务的当前负载。参数 records：完整任务；mapping：字段映射；members：人员 ID 集合；exclude_id：排除本次任务。返回值：各人分数、任务数及缺分记录。"""
    loads = {uid: {'score': 0, 'count': 0, 'missing': []} for uid in members}
    for record in records:
        f = record['fields']
        if record['record_id'] == exclude_id or any(checked(f.get(mapping[k])) for k in ('cancelled', 'is_test')) or f.get(mapping['completed_at']):
            continue
        key = 'assignee' if checked(f.get(mapping['leader_confirmed'])) else 'recommended_assignee'
        for uid in set(user_ids(f.get(mapping[key]))):
            if uid not in loads:
                continue
            value = load_score(f.get(mapping['load_score']))
            loads[uid]['count'] += 1
            if value is None:
                loads[uid]['missing'].append(record['record_id'])
            else:
                loads[uid]['score'] += value
    return loads


def product_owner(demand, records, config):
    """功能说明：按完整货号及拼款上下文定位归属。参数 demand：需求文本；records：分配记录；config：字段和提取规则。返回值：人员昵称或空值、说明、是否允许老款兜底。"""
    text = ' '.join(plain(demand.get(k)) for k in ('title', 'description', 'platform', 'notes'))
    codes = set(re.findall(config['code_pattern'], text.upper()))
    if not codes:
        return None, '需求缺少可识别货号，请补充货号。', False
    bundle = bool(re.search(config['bundle_pattern'], text)) or len(codes) > 1
    matches = []
    for record in records:
        title = plain(record['fields'].get(config['title_field']))
        row_codes = set(re.findall(config['code_pattern'], title.upper()))
        if codes <= row_codes:
            matches.append((record, title, row_codes))
    if not matches:
        old = bool(re.search(config['old_pattern'], text))
        return None, '分配表未找到该货号归属。', old
    selected = [r for r in matches if (len(r[2]) > 1) == bundle and r[2] == codes]
    if not selected:
        return None, '货号存在，但单款／拼款范围未能唯一对应，请补充完整组合。', False
    platform = plain(demand.get('platform'))
    applicable = []
    for row in selected:
        limits = [p for p in config['platform_aliases'] if p in row[1]]
        if limits and not any(p in platform for p in limits):
            continue
        applicable.append(row)
    if not applicable:
        return None, '分配记录有平台限制，需求平台尚未对应。', False
    owners = {plain(r[0]['fields'].get(config['owner_field'])) for r in applicable}
    if len(owners) != 1 or not next(iter(owners)):
        return None, '该货号存在不同认领人员或认领信息为空，需人工确认。', False
    return owners.pop(), '商品归属记录：' + '、'.join(r[0]['record_id'] for r in applicable), False


def shop_owner(demand, shops):
    """功能说明：按部门及店铺名称定位负责团队。参数 demand：需求文本；shops：店铺配置。返回值：唯一店铺配置或空值。"""
    text = re.sub(r'\s+', '', ' '.join(plain(demand.get(k)) for k in ('platform', 'title', 'description', 'notes'))).casefold()
    matches = [s for s in shops if s['department'] == plain(demand.get('department')) and
               any(re.sub(r'\s+', '', a).casefold() in text for a in s['aliases'])]
    return matches[0] if len(matches) == 1 else None


def recommend(record_id, fields, demand, rule, mapping, feishu):
    """功能说明：生成推荐和待提交的轮换状态。参数 record_id：当前记录；fields：当前字段；demand：需求；rule：命中规则或空；mapping：字段映射；feishu：资源客户端。返回值：字段更新与可选轮换状态。"""
    result_field, person_field = mapping['recommendation_result'], mapping['recommended_assignee']
    def answer(reason, uid=None, state=None):
        logger.info('人员推荐完成：record_id=%s recommended=%s', record_id, bool(uid))
        return {result_field: reason, person_field: [{'id': uid}] if uid else []}, state

    if not rule or load_score(rule.get('load_score')) is None:
        return answer('暂未推荐：规则未准确匹配或任务负载分无效。')
    path = feishu.settings.fields_path.parent / 'recommendation.json'
    logger.info('加载人员推荐配置：%s', path)
    config = json.loads(path.read_text(encoding='utf-8'))
    names = config['members']
    if len(set(names.values())) != len(names) or set(config['rotation']) != set(names) or len(config['rotation']) != len(names):
        raise ValueError('人员映射或轮换顺序有重复、缺失')
    current = {uid for row in feishu.get_records(config['member_table'])
               for uid in user_ids(row['fields'].get(config['member_field']))}
    if set(names.values()) != current:
        raise ValueError('成员负荷表与推荐人员配置不一致')
    loads = workloads(feishu.get_records(feishu.settings.table_id), mapping, current, record_id)
    def describe(candidates):
        return '；'.join(f"{n}：" + ('缺分（' + '、'.join(loads[names[n]]['missing']) + '）' if loads[names[n]]['missing'] else
                                    f"{loads[names[n]]['score']:g} 分／{loads[names[n]]['count']} 单") for n in candidates)

    method = rule.get('assignment', '')
    candidates = list(config['rotation'])
    source = f"分配方式：{method}\n"
    if method == '人工分配':
        return answer(source + '由组长确定负责人。候选负载参考：' + describe(candidates))
    if method.startswith('固定人员'):
        match = re.fullmatch(r'固定人员[（(](.+)[）)]', method)
        candidates = re.split(r'[\s、,，]+', match[1].strip()) if match else []
        if not candidates or len(set(candidates)) != len(candidates) or any(n not in names for n in candidates):
            return answer(source + '固定人员名单缺失或包含未配置成员，需人工确认。')
    elif method == '找对应人员':
        route = config['ownership_routes'].get(rule['record_id'])
        if route == 'shop':
            shop = shop_owner(demand, config['shops'])
            if not shop:
                return answer(source + '部门和店铺未能唯一对应，请补充具体店铺名称。')
            owner, backups = shop['primary'], shop['backup']
            if any(n not in names for n in [owner] + backups):
                return answer(source + '店铺团队存在未配置成员，需人工确认。')
            return answer(source + f"推荐：{owner}；按店铺主负责人归属。\n协作人员：{'、'.join(backups)}\n" + describe([owner] + backups), names[owner])
        if route != 'product':
            return answer(source + '该规则尚未配置归属查询路径，需人工确认。')
        p = config['product']
        owner, reason, fallback = product_owner(demand, feishu.get_records(p['table_id'], p['base_token']), p)
        if owner in config['excluded_owners']:
            return answer(source + reason + f'\n原认领人员{owner}已不属于当前美工团队，需人工分配。')
        if owner:
            if owner not in names:
                return answer(source + reason + '\n认领人员不在当前成员名单，需人工分配。')
            return answer(source + reason + f'\n推荐：{owner}；沿用商品归属。\n' + describe([owner]), names[owner])
        if not fallback:
            return answer(source + reason)
        source += '明确为老款且未找到归属，按当前负载分配。\n'
    elif method != '轮流':
        return answer(source + '匹配方式为空或不受支持，需人工确认。')

    if any(loads[names[n]]['missing'] for n in candidates):
        return answer(source + '候选负载存在缺分，暂不比较。\n' + describe(candidates))
    # 同分按既定顺序轮换；当前记录的预占已从比较中排除。
    minimum = min(loads[names[n]]['score'] for n in candidates)
    tied = [n for n in config['rotation'] if n in candidates and loads[names[n]]['score'] == minimum]
    state_path = feishu.settings.rules_cache_path.parent / 'rotation.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    pool = '|'.join(sorted(names[n] for n in candidates))
    previous = state.get(pool)
    if previous in config['rotation']:
        index = config['rotation'].index(previous) + 1
        order = config['rotation'][index:] + config['rotation'][:index]
        tied.sort(key=order.index)
    existing = user_ids(fields.get(person_field))
    winner = next((n for n in tied if names[n] in existing), tied[0])
    state[pool] = winner
    # 测试需求不推进正式轮换。
    pending = None if checked(fields.get(mapping['is_test'])) else (state_path, state)
    return answer(source + f'推荐：{winner}\n候选负载：' + describe(candidates) +
                  f"\n本任务：{rule['load_score']:g} 分；推荐后：{minimum + rule['load_score']:g} 分。\n依据：负载最低，同分按顺序轮换。", names[winner], pending)


def save_rotation(pending):
    """在飞书回写成功后原子保存轮换位置。"""
    if pending:
        path, state = pending
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
