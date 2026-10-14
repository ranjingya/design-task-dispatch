"""根据平台、店铺别名与需求证据识别店铺。"""

import json
import logging
import re
import unicodedata

from app.ai import AIClient
from app.rules import plain

logger = logging.getLogger(__name__)


def compact(value):
    """统一全半角、大小写，忽略空格与标点。"""
    return ''.join(c for c in unicodedata.normalize('NFKC', plain(value)).casefold() if c.isalnum())


def normalize(value, platforms):
    """功能说明：归一化平台名称。参数 value：输入文字；platforms：标准平台到别名列表的映射。返回值：归一化文字。"""
    text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', plain(value)).casefold())
    replacements = sorted(((compact(alias), compact(name)) for name, aliases in platforms.items()
                           for alias in [name, *aliases]), key=lambda pair: -len(pair[0]))
    if replacements:
        mapping = dict(replacements)
        patterns = [r'(?<![a-z0-9])' + re.escape(a) + r'(?![a-z0-9])' if a.isascii() else re.escape(a) for a in mapping]
        text = re.sub('|'.join(patterns), lambda m: mapping[m[0]], text)
    return compact(text)


def sources_for(demand):
    """提取需求字段和已读取材料，保持字段边界供模型引用原文。"""
    sources = {key: plain(demand.get(key)) for key in ('platform', 'title', 'description', 'notes')}
    for index, material in enumerate(demand.get('materials') or []):
        if isinstance(material, dict):
            sources[f'material_{index}_source'] = plain(material.get('source'))
            sources[f'material_{index}_text'] = plain(material.get('text'))
    return {key: value for key, value in sources.items() if value}


def exact_shops(sources, shops, platforms):
    """按单个字段内的完整别名匹配，避免拼接字段产生虚假的店铺名称。"""
    texts = [normalize(value, platforms) for value in sources.values()]
    return [shop for shop in shops if any(
        normalize(alias, platforms) and any(normalize(alias, platforms) in text for text in texts)
        for alias in shop['aliases']
    )]


def validate_catalog(shops, platforms):
    """校验店铺标识、名称、平台和别名配置。"""
    if not isinstance(platforms, dict) or any(
        not isinstance(name, str) or not compact(name) or not isinstance(aliases, list) or
        any(not isinstance(a, str) or not compact(a) for a in aliases)
        for name, aliases in platforms.items()
    ):
        raise ValueError('店铺平台别名配置无效')
    identities = {}
    for name, aliases in platforms.items():
        for alias in [name, *aliases]:
            key = compact(alias)
            if key in identities and identities[key] != name:
                raise ValueError('平台别名同时对应多个平台')
            identities[key] = name
    seen = set()
    for shop in shops:
        if not isinstance(shop, dict) or any(not isinstance(shop.get(k), str) or not shop[k].strip()
                                              for k in ('id', 'name', 'department')):
            raise ValueError('店铺配置缺少标识、名称或部门')
        if shop['id'] in seen or shop.get('platform', '') not in ('', *platforms):
            raise ValueError('店铺标识重复或平台未配置')
        seen.add(shop['id'])
        if not isinstance(shop.get('aliases'), list) or not shop['aliases'] or any(
            not isinstance(a, str) or not compact(a) for a in shop['aliases']
        ):
            raise ValueError('店铺别名必须为非空文字列表')


def resolve_shop(demand, config, settings=None):
    """功能说明：按别名匹配并可使用 AI 消歧。参数 demand：需求和材料；config：店铺及平台配置；settings：可选模型配置。返回值：(唯一店铺或空值，解释，候选店铺列表)。"""
    shops = config['shops']
    platforms = config.get('shop_platforms', {})
    enabled = config.get('shop_ai_fallback', False)
    if type(enabled) is not bool:
        raise ValueError('shop_ai_fallback 必须为布尔值')
    # 旧配置仍可执行明确别名匹配；AI 识别必须具备稳定的店铺 ID。
    if enabled or platforms:
        validate_catalog(shops, platforms)
    candidates = [s for s in shops if s['department'] == plain(demand.get('department'))]
    sources = sources_for(demand)
    platform_text = sources.get('platform', '') or ' '.join(sources.get(k, '') for k in ('title', 'description'))
    normalized = normalize(platform_text, platforms)
    known = [name for name in platforms if compact(name) in normalized]
    if len(known) > 1:
        return None, '需求涉及多个平台，请确认本次目标店铺', candidates
    # 文档可能包含竞品或参考店铺，交给模型结合上下文识别，不能仅凭出现名称归属。
    matches = exact_shops({k: v for k, v in sources.items() if not k.startswith('material_')}, candidates, platforms)
    if known:
        candidates = [s for s in candidates if s.get('platform', '') in ('', known[0])]
        explicit_conflicts = [s for s in matches if s not in candidates and any(
            compact(s.get('platform', '')) in normalize(alias, platforms) and
            exact_shops({k: v for k, v in sources.items() if not k.startswith('material_')},
                        [dict(s, aliases=[alias])], platforms)
            for alias in s['aliases']
        )]
        if explicit_conflicts:
            return None, '店铺名称与填写的平台冲突，请确认目标店铺', candidates
        matches = [s for s in matches if s in candidates]
    if len(matches) == 1:
        shop = matches[0]
        name = shop.get('name', shop['aliases'][0])
        logger.info('店铺识别 方式=别名 店铺=%s', name)
        return shop, f'名称／别名匹配：{name}', candidates
    if not candidates:
        return None, '当前部门和平台没有已配置店铺，请补充店铺配置', []
    fallback = '需求同时提到多家店铺，请确认本次目标店铺' if matches else '缺少可唯一识别的店铺名称，请补充店铺名称或用途'
    if not enabled or settings is None:
        return None, fallback, candidates
    if demand.get('material_issues'):
        return None, '材料未完整读取，请补齐材料后确认店铺', candidates
    try:
        logger.info('店铺识别 方式=AI消歧 候选数=%d', len(candidates))
        settings.validate_ai()
        prompt = settings.prompt_path.with_name('identify_shop.md').read_text(encoding='utf-8')
        if not prompt.strip():
            raise ValueError('店铺识别提示词为空')
        payload = {'sources': sources, 'candidates': [
            {key: shop.get(key, '') for key in ('id', 'name', 'department', 'platform', 'aliases')}
            for shop in candidates], 'confirmed_platform': known[0] if known else None}
        result, _ = AIClient(settings)._complete([
            {'role': 'system', 'content': prompt},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
        ])
        if not isinstance(result, dict) or result.get('status') not in ('matched', 'needs_review'):
            raise ValueError('店铺识别状态无效')
        reason = result.get('reason')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError('店铺识别依据无效')
        if result['status'] == 'needs_review':
            if result.get('shop_id') is not None:
                raise ValueError('待确认结果不能包含确定店铺')
            return None, reason, candidates
        shop = next((s for s in candidates if s['id'] == result.get('shop_id')), None)
        evidence = result.get('evidence')
        if shop is None or not isinstance(evidence, dict):
            raise ValueError('模型返回未知店铺或缺少原文证据')
        source, quote = evidence.get('source'), evidence.get('quote')
        if not isinstance(source, str) or source == 'platform' or source not in sources or not isinstance(quote, str) or not quote.strip() or len(quote) > 500 or quote not in sources[source]:
            raise ValueError('店铺识别证据不属于本次需求原文')
        if compact(quote) in {compact(name) for name in platforms} or normalize(quote, platforms) in {compact(name) for name in platforms}:
            raise ValueError('仅凭平台不能确定店铺')
        logger.info('店铺识别 方式=AI 店铺=%s 证据字段=%s', shop['name'], source)
        return shop, f'AI识别：{reason}；依据“{quote}”', candidates
    except (ValueError, RuntimeError, OSError):
        logger.warning('店铺 AI 消歧失败，保留人工确认', exc_info=True)
        return None, '店铺识别暂不可用，请人工确认或重试', candidates
