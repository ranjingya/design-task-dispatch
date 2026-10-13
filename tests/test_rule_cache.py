"""验证每日快照复用、跨日刷新和异常降级。"""

import json
from unittest.mock import Mock

import pytest

from app.config import Settings
from app.feishu import FeishuClient
from app.rule_cache import load_daily_rules


@pytest.fixture
def client(tmp_path):
    settings = Settings('app', 'secret', 'base', 'table', 'token', tmp_path / 'fields.json',
                        rules_table_id='rules', rules_cache_path=tmp_path / 'cache/rules.json')
    client = Mock(spec=FeishuClient)
    client.settings = settings
    client.get_rules.return_value = [{"record_id": "recOne", "fields": {
        "工作类型": "示例", "包含的工作": "制作示例素材", "适用部门": "测试部",
        "默认ABC分类": "B常规设计", "分类调整说明": "", "匹配方式": "轮流", "人员负载分数": 3}}]
    return client


def test_daily_reuse_restart_and_next_day(client, monkeypatch):
    monkeypatch.setattr('app.rule_cache.today', lambda: '2026-10-12')
    first = load_daily_rules(client)
    assert load_daily_rules(client) == first
    client.get_rules.assert_called_once()
    restarted = Mock(spec=FeishuClient)
    restarted.settings = client.settings
    assert load_daily_rules(restarted) == first
    restarted.get_rules.assert_not_called()
    monkeypatch.setattr('app.rule_cache.today', lambda: '2026-10-13')
    assert load_daily_rules(client) == first
    assert client.get_rules.call_count == 2
    assert json.loads(client.settings.rules_cache_path.read_text())['date'] == '2026-10-13'


def test_failed_refresh_preserves_snapshot_and_retries(client, monkeypatch):
    monkeypatch.setattr('app.rule_cache.today', lambda: '2026-10-12')
    first = load_daily_rules(client)
    before = client.settings.rules_cache_path.read_bytes()
    monkeypatch.setattr('app.rule_cache.today', lambda: '2026-10-13')
    client.get_rules.side_effect = RuntimeError('服务不可用')
    assert load_daily_rules(client) == first
    assert client.settings.rules_cache_path.read_bytes() == before
    client.get_rules.side_effect = None
    assert load_daily_rules(client) == first
    assert client.get_rules.call_count == 3


def test_no_cache_and_failure_raises(client):
    client.get_rules.side_effect = RuntimeError('服务不可用')
    with pytest.raises(RuntimeError):
        load_daily_rules(client)


@pytest.mark.parametrize('content', ['not json', '[]', '{"source":{"base_token":"another","table_id":"rules"}}'])
def test_corrupt_or_wrong_source_reloads(client, content):
    path = client.settings.rules_cache_path
    path.parent.mkdir()
    path.write_text(content)
    assert load_daily_rules(client)[0]['record_id'] == 'recOne'
    client.get_rules.assert_called_once()


def test_invalid_refresh_does_not_replace_valid_snapshot(client, monkeypatch):
    monkeypatch.setattr('app.rule_cache.today', lambda: '2026-10-12')
    first = load_daily_rules(client)
    monkeypatch.setattr('app.rule_cache.today', lambda: '2026-10-13')
    client.get_rules.return_value = []
    assert load_daily_rules(client) == first
    assert json.loads(client.settings.rules_cache_path.read_text())['date'] == '2026-10-12'


def test_atomic_write_failure_keeps_old_snapshot(client, monkeypatch):
    monkeypatch.setattr('app.rule_cache.today', lambda: '2026-10-12')
    first = load_daily_rules(client)
    old = client.settings.rules_cache_path.read_bytes()
    monkeypatch.setattr('app.rule_cache.today', lambda: '2026-10-13')
    monkeypatch.setattr('app.rule_cache.os.replace', Mock(side_effect=OSError('磁盘错误')))
    assert load_daily_rules(client) == first
    assert client.settings.rules_cache_path.read_bytes() == old
    assert list(client.settings.rules_cache_path.parent.glob('.rules-*')) == []
