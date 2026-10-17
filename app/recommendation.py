"""按规则、业务归属和实时负载生成可解释的人员推荐。"""

import json
import logging
import re
from pathlib import Path

from app.rules import load_score, plain
from app.shop_matching import resolve_shop
from app.task_logging import task_details

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
    """功能说明：计算未结束任务的当前负载。参数 records：完整任务；mapping：字段映射；members：人员 ID 集合；exclude_id：排除本次任务。返回值：各人分数、任务数及缺分记录。"""
    loads = {uid: {'score': 0, 'count': 0, 'missing': []} for uid in members}
    for record in records:
        f = record['fields']
        if record['record_id'] == exclude_id or checked(f.get(mapping['cancelled'])) or f.get(mapping['completed_at']):
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
        if limits and not any(platform_policy(platform, [{'aliases': [p] + config.get('platform_short_names', {}).get(p, [])}]) for p in limits):
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
    return resolve_shop(demand, {'shops': shops})[0]


def platform_policy(platform, policies):
    """功能说明：按提报平台唯一匹配策略。参数 platform：平台文本；policies：平台别名与动作配置。返回值：唯一策略或空值。"""
    text = plain(platform).casefold()
    matches = []
    for policy in policies:
        for alias in policy['aliases']:
            pattern = re.escape(alias.casefold())
            if alias.isascii():
                pattern = r'(?<![a-z0-9])' + pattern + r'(?![a-z0-9])'
            if re.search(pattern, text):
                matches.append(policy)
                break
    return matches[0] if len(matches) == 1 else None


def format_recommendation(method, reason, people, score):
    """功能说明：生成四行推荐文本。参数 method：推荐方式；reason：规则或异常原因；people：人员负载摘要；score：任务分数或空值。返回值：四行展示文本。"""
    compact = lambda value: ' '.join(str(value).split())
    last = f'本任务 {score:g} 分' if score is not None else '本任务分数待确定'
    return '\n\n'.join([compact(method), compact(reason), compact(people), last])


def recommend(record_id, fields, demand, rule, mapping, feishu):
    """功能说明：生成推荐和待提交的轮换状态。参数 record_id：当前记录；fields：当前字段；demand：需求；rule：命中规则或空；mapping：字段映射；feishu：资源客户端。返回值：字段更新与可选轮换状态。"""
    result_field, person_field = mapping['recommendation_result'], mapping['recommended_assignee']
    score = load_score(rule.get('load_score')) if rule else None
    names, loads, candidates = {}, {}, []

    def answer(reason, uid=None, state=None, mode='人工分配', people=None):
        """按传入顺序回写单人或多人推荐，重复人员仅保留第一次出现的位置。"""
        ids = list(dict.fromkeys(uid if isinstance(uid, list) else [uid] if uid else []))
        labels = {value: name for name, value in names.items()}
        task_details(recommended='、'.join(labels[value] for value in ids) if ids else '人工分配')
        logger.debug('人员推荐完成：record_id=%s recommended=%s reason=%s', record_id, bool(uid), reason)
        summary = people if people is not None else (describe(candidates) if loads else '人员负载未比较')
        text = format_recommendation(mode, reason, summary, score)
        return {result_field: text, person_field: [{'id': value} for value in ids]}, state

    if not rule or score is None:
        return answer('未准确匹配规则，暂时无法推荐' if not rule else '规则负载分缺失或无效，请补充分数')
    path = feishu.settings.fields_path.parent / 'recommendation.json'
    logger.debug('加载人员推荐配置：%s', path)
    config = json.loads(path.read_text(encoding='utf-8'))
    settings = feishu.settings
    if not settings.members_table_id:
        raise ValueError('请配置 FEISHU_MEMBERS_TABLE_ID')
    names = config.get('members')
    routes = config.get('ownership_routes')
    if not isinstance(names, dict) or not names or any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
        for k, v in names.items()
    ):
        raise ValueError('推荐配置 members 必须为非空人员映射')
    if not isinstance(routes, dict) or any(
        not isinstance(k, str) or not k.strip() or v not in ('product', 'shop', 'product_platform')
        for k, v in routes.items()
    ):
        raise ValueError('推荐配置 ownership_routes 必须映射到 product、shop 或 product_platform')
    if len(set(names.values())) != len(names) or set(config['rotation']) != set(names) or len(config['rotation']) != len(names):
        raise ValueError('人员映射或轮换顺序有重复、缺失')
    current = {uid for row in feishu.get_records(settings.members_table_id)
               for uid in user_ids(row['fields'].get(config['member_field']))}
    if set(names.values()) != current:
        raise ValueError('成员负荷表与推荐人员配置不一致')
    loads = workloads(feishu.get_records(feishu.settings.table_id), mapping, current, record_id)
    def describe(candidates):
        return ' ｜ '.join(f"{n} " + ('缺分' if loads[names[n]]['missing'] else
                                      f"{loads[names[n]]['score']:g}") for n in candidates)

    method = rule.get('assignment', '')
    candidates = list(config['rotation'])
    source = '全体美工负载最低优先，同分轮流'
    display_method = '轮流'
    if method == '人工分配':
        return answer('该类任务由组长结合人员能力和负载安排')
    if method.startswith('固定人员'):
        match = re.fullmatch(r'固定人员[（(](.+)[）)]', method)
        candidates = re.split(r'[\s、,，]+', match[1].strip()) if match else []
        if not candidates or len(set(candidates)) != len(candidates) or any(n not in names for n in candidates):
            return answer('固定人员名单缺失或包含未配置成员，需人工确认', people=describe(list(config['rotation'])))
        source = '指定人员负载最低优先，同分轮流'
        display_method = '固定人员'
    elif method == '找对应人员':
        route = routes.get(rule['record_id'])
        if route == 'shop':
            shop, basis, shop_candidates = resolve_shop(demand, config, settings)
            if not shop:
                choices = '、'.join(s.get('name', s['aliases'][0]) for s in shop_candidates)
                logger.warning('店铺识别待确认：%s', basis)
                return answer(basis + (f'；候选店铺：{choices}' if choices else ''))
            owner, backups = shop['primary'], shop['backup']
            if any(n not in names for n in [owner] + backups):
                return answer('店铺团队存在未配置成员，需人工确认')
            team = list(dict.fromkeys([owner] + backups))
            return answer(f"{shop.get('name', shop['aliases'][0])}，{basis}；按主副负责人顺序推荐",
                          [names[name] for name in team], mode='对应人员', people=describe(team))
        policy = None
        if route == 'product_platform':
            policies = config.get('product_platform_rules', {}).get(rule['record_id'], [])
            if not isinstance(policies, list) or any(
                not isinstance(p, dict) or p.get('action') not in ('owner', 'exclude_owner') or
                not isinstance(p.get('aliases'), list) or not p['aliases'] or
                any(not isinstance(a, str) or not a.strip() for a in p['aliases'])
                for p in policies
            ):
                raise ValueError('product_platform_rules 平台策略配置无效')
            policy = platform_policy(demand.get('platform'), policies)
            if not policy:
                return answer('未能唯一确定平台，请补充明确的平台信息')
            logger.debug('平台推荐策略：record_id=%s action=%s', record_id, policy['action'])
        if route not in ('product', 'product_platform'):
            return answer('该规则尚未配置归属查询路径，需人工确认')
        if not settings.product_base_token or not settings.product_table_id:
            raise ValueError('请配置 FEISHU_PRODUCT_BASE_TOKEN 和 FEISHU_PRODUCT_TABLE_ID')
        p = config['product']
        owner, reason, fallback = product_owner(demand, feishu.get_records(settings.product_table_id, settings.product_base_token), p)
        logger.debug('商品归属查询：record_id=%s detail=%s', record_id, reason)
        if owner in config['excluded_owners']:
            return answer(f'原认领人{owner}不在美工名单，由组长安排')
        if owner:
            if owner not in names:
                return answer(f'原认领人{owner}不在美工名单，由组长安排')
            if policy and policy['action'] == 'exclude_owner':
                candidates = [n for n in candidates if n != owner]
                source = f'排除原认领人{owner}，负载最低优先，同分轮流'
            else:
                text = ' '.join(plain(demand.get(k)) for k in ('title', 'description', 'platform', 'notes'))
                codes = '、'.join(dict.fromkeys(re.findall(p['code_pattern'], text.upper())))
                return answer(f"{plain(demand.get('platform'))} {codes}，沿用数据包认领人", names[owner],
                              mode='对应人员', people=describe([owner]))
        elif policy or not fallback:
            return answer(reason)
        else:
            source = '老款未找到归属，负载最低优先，同分轮流'
    elif method != '轮流':
        return answer('推荐方式为空或不受支持，需人工确认')

    if not candidates:
        return answer('没有可推荐成员，需人工分配', people='暂无候选人员')
    missing = [n for n in candidates if loads[names[n]]['missing']]
    available = [n for n in candidates if n not in missing]
    if missing:
        sources = '；'.join(f"{n}={','.join(loads[names[n]]['missing'])}" for n in missing)
        logger.warning('候选负载缺分 跳过=%s 来源=%s', '、'.join(missing), sources)
    if not available:
        return answer('全部候选人员均有任务缺分，请补充分数后重试')
    if missing:
        source += f"；{'、'.join(missing)}任务缺分，暂不参与比较"
    # 同分按既定顺序轮换；当前记录的预占已从比较中排除。
    minimum = min(loads[names[n]]['score'] for n in available)
    tied = [n for n in config['rotation'] if n in available and loads[names[n]]['score'] == minimum]
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
    logger.debug('准备更新轮换位置：pool=%s winner=%s', pool, winner)
    pending = (state_path, state)
    return answer(source, names[winner], pending, mode=display_method)



def save_rotation(pending):
    """在飞书回写成功后原子保存轮换位置。"""
    if pending:
        path, state = pending
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
