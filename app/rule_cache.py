"""按自然日持久化规则快照，刷新异常时保留最后可用规则。"""

import json
import logging
import os
import tempfile
from datetime import datetime

from app.feishu import FeishuClient
from app.rules import normalize_rules

logger = logging.getLogger(__name__)


def today() -> str:
    """按服务所在时区返回自然日。"""
    return datetime.now().astimezone().date().isoformat()


def load_daily_rules(feishu: FeishuClient) -> list[dict]:
    """功能说明：读取当天规则快照或刷新飞书规则。参数 feishu：含缓存路径与表配置的客户端。返回值：校验后的标准化规则；刷新失败时返回旧快照，无可用快照则抛错。"""
    settings = feishu.settings
    path = settings.rules_cache_path
    source = {"base_token": settings.base_token, "table_id": settings.rules_table_id}
    cached_rules, cached_day = None, None
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict) or snapshot.get("source") != source:
            raise ValueError("规则缓存来源不一致")
        cached_rules = normalize_rules(snapshot["records"], settings.rule_fields_path)
        cached_day = snapshot["date"]
        if not isinstance(cached_day, str):
            raise ValueError("规则缓存日期无效")
    except FileNotFoundError:
        logger.debug("规则缓存不存在，准备首次加载")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        cached_rules = None
        logger.warning("规则缓存不可用，准备重新加载")
    current_day = today()
    if cached_rules is not None and cached_day == current_day:
        logger.debug("复用本地规则快照：date=%s count=%d", cached_day, len(cached_rules))
        return cached_rules
    try:
        logger.debug("刷新每日规则快照：date=%s", current_day)
        records = feishu.get_rules()
        rules = normalize_rules(records, settings.rule_fields_path)
        snapshot = {"source": source, "date": current_day, "records": records}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".rules-", delete=False) as file:
                temporary = file.name
                json.dump(snapshot, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        logger.debug("规则快照保存完成：date=%s count=%d path=%s", current_day, len(rules), path)
        return rules
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, AttributeError):
        if cached_rules is None:
            logger.error("规则刷新失败且无可用快照")
            raise
        logger.warning("规则刷新失败，继续使用旧快照：date=%s count=%d", cached_day, len(cached_rules), exc_info=True)
        return cached_rules
