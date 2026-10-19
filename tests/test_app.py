"""接口、外部配置和后台任务边界测试。"""

import json
import threading
from unittest.mock import Mock

import pytest

from app.config import Settings, load_fields
from app.dispatch import Dispatcher, process_record
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    """使用隔离配置，不读取真实应用密钥。"""
    path = tmp_path / "fields.json"
    path.write_text(json.dumps({"title": "任务名称", "description": "需求说明"}))
    return Settings("app", "secret", "base", "table", "test-token", path)


def test_webhook_auth_and_validation(settings):
    dispatcher = Mock()
    dispatcher.submit.return_value = True
    client = create_app(settings, dispatcher).test_client()
    assert client.get("/health").status_code == 200
    assert client.post("/webhook/task", json={"record_id": "rec123"}).status_code == 401
    headers = {"X-Webhook-Token": "test-token"}
    for body in (None, [], {}, {"record_id": "../invalid"}, {"record_id": 123}):
        assert client.post("/webhook/task", json=body, headers=headers).status_code == 400
    dispatcher.submit.assert_not_called()
    assert client.post("/webhook/task", json={"record_id": "rec123"}, headers=headers).status_code == 202
    dispatcher.submit.return_value = False
    assert client.post("/webhook/task", json={"record_id": "rec123"}, headers=headers).status_code == 200


def test_invalid_config_returns_retryable_response(settings):
    dispatcher = Dispatcher(settings, runner=Mock())
    try:
        client = create_app(settings, dispatcher).test_client()
        settings.fields_path.write_text("invalid")
        response = client.post("/webhook/task", json={"record_id": "rec123"},
                               headers={"X-Webhook-Token": "test-token"})
        assert response.status_code == 503
        dispatcher.runner.assert_not_called()
    finally:
        dispatcher.close()


def test_config_hot_reload_and_task_snapshot(settings):
    entered, release = threading.Event(), threading.Event()
    seen = []

    def runner(record_id, mapping, feishu):
        """控制任务时序，检查修改配置对新旧任务的作用。"""
        if record_id == "recFirst":
            entered.set()
            assert release.wait(5)
        seen.append(mapping["title"])

    dispatcher = Dispatcher(settings, runner)
    try:
        assert dispatcher.submit("recFirst")
        assert entered.wait(5)
        assert not dispatcher.submit("recFirst")
        replacement = settings.fields_path.with_suffix(".tmp")
        replacement.write_text(json.dumps({"title": "新标题", "description": "需求说明"}))
        replacement.replace(settings.fields_path)
        assert dispatcher.submit("recSecond")
    finally:
        release.set()
        dispatcher.close()
    assert seen == ["任务名称", "新标题"]
    assert not dispatcher.submit("recFirst")


def test_failed_record_can_be_retried(settings):
    runner = Mock(side_effect=RuntimeError("模拟外部失败"))
    dispatcher = Dispatcher(settings, runner)
    try:
        assert dispatcher.submit("recFail")
        dispatcher._executor.submit(lambda: None).result(timeout=5)
        runner.side_effect = None
        assert dispatcher.submit("recFail")
    finally:
        dispatcher.close()
    assert runner.call_count == 2


def test_notification_endpoint_validation_and_failure(settings, monkeypatch):
    notify = Mock(return_value='sent_or_duplicate')
    monkeypatch.setattr('app.main.notify_record', notify)
    dispatcher = Mock()
    client = create_app(settings, dispatcher).test_client()
    body = {'record_id': 'recTest', 'event': 'leader_confirmed'}
    headers = {'X-Webhook-Token': 'test-token'}
    assert client.post('/webhook/notify', json=body).status_code == 401
    for invalid in [[], {}, body | {'event': []}, body | {'event': 'unknown'}, body | {'record_id': '../bad'}]:
        assert client.post('/webhook/notify', json=invalid, headers=headers).status_code == 400
    notify.assert_not_called()
    for event in ['leader_confirmed', 'assignee_confirmed', 'deadline_reminder']:
        assert client.post('/webhook/notify', json=body | {'event': event}, headers=headers).status_code == 200
        assert notify.call_args.args[:2] == ('recTest', event)
    notify.side_effect = RuntimeError('通知失败')
    assert client.post('/webhook/notify', json=body, headers=headers).status_code == 503
    dispatcher.submit.assert_not_called()
