"""飞书瞬时故障重试与手动重试入口测试。"""

from unittest.mock import Mock

import httpx
import pytest

from app.cli import main
from app.feishu import FeishuClient


def response(status=200, payload=None):
    return httpx.Response(status, json=payload if payload is not None else {'code': 0, 'data': {}})


def test_transient_failures_recover(monkeypatch, caplog):
    request = Mock(side_effect=[httpx.ReadTimeout('secret'), response(503), response()])
    sleep = Mock()
    monkeypatch.setattr('app.feishu.httpx.request', request)
    monkeypatch.setattr('app.feishu.time.sleep', sleep)
    assert FeishuClient._request('GET', '/example')['code'] == 0
    assert request.call_count == 3
    assert [x.args[0] for x in sleep.call_args_list] == [1, 2]
    assert 'ReadTimeout' in caplog.text and 'secret' not in caplog.text


def test_retry_exhaustion_is_bounded(monkeypatch):
    request = Mock(side_effect=httpx.ConnectTimeout('secret'))
    monkeypatch.setattr('app.feishu.httpx.request', request)
    monkeypatch.setattr('app.feishu.time.sleep', Mock())
    with pytest.raises(RuntimeError, match='ConnectTimeout'):
        FeishuClient._request('GET', '/example')
    assert request.call_count == 3


@pytest.mark.parametrize('value', [response(403), response(payload={'code': 99991672}), response(400)])
def test_permanent_errors_are_not_retried(monkeypatch, value):
    request = Mock(return_value=value)
    monkeypatch.setattr('app.feishu.httpx.request', request)
    with pytest.raises(RuntimeError):
        FeishuClient._request('GET', '/example')
    assert request.call_count == 1


@pytest.mark.parametrize('method,path,attempts', [('POST', '/creates', 1), ('PUT', '/records/recA', 3),
                                                ('POST', '/auth/v3/tenant_access_token/internal', 3)])
def test_only_safe_operations_are_replayed(monkeypatch, method, path, attempts):
    request = Mock(side_effect=httpx.ReadTimeout('timeout'))
    monkeypatch.setattr('app.feishu.httpx.request', request)
    monkeypatch.setattr('app.feishu.time.sleep', Mock())
    with pytest.raises(RuntimeError):
        FeishuClient._request(method, path)
    assert request.call_count == attempts


@pytest.mark.parametrize('status,payload,expected', [(202, {'status': 'accepted'}, 0),
    (200, {'status': 'duplicate'}, 0), (401, {}, 1), (503, {}, 1), (200, {}, 1)])
def test_cli_uses_existing_service_queue(monkeypatch, status, payload, expected):
    monkeypatch.setattr('app.cli.Settings.from_env', Mock(return_value=Mock(webhook_token='secret')))
    post = Mock(return_value=httpx.Response(status, json=payload))
    monkeypatch.setattr('app.cli.httpx.post', post)
    assert main(['retry', 'recExample']) == expected
    assert post.call_args.args == ('http://127.0.0.1:8000/webhook/task',)
    assert post.call_args.kwargs['json'] == {'record_id': 'recExample'}


def test_invalid_cli_record_never_sends(monkeypatch):
    post = Mock()
    monkeypatch.setattr('app.cli.httpx.post', post)
    with pytest.raises(SystemExit):
        main(['retry', 'invalid'])
    post.assert_not_called()


@pytest.mark.parametrize('status', [200, 400])
def test_bitable_not_ready_is_retryable(monkeypatch, status):
    request = Mock(side_effect=[response(status, {'code': 1254607}), response()])
    monkeypatch.setattr('app.feishu.httpx.request', request)
    monkeypatch.setattr('app.feishu.time.sleep', Mock())
    assert FeishuClient._request('GET', '/example')['code'] == 0
    assert request.call_count == 2


def test_cli_can_retry_notification_without_ai(monkeypatch):
    settings = Mock()
    monkeypatch.setattr('app.cli.Settings.from_env', Mock(return_value=settings))
    monkeypatch.setattr('app.cli.load_fields', Mock(return_value={'title': '标题'}))
    client = Mock()
    monkeypatch.setattr('app.cli.FeishuClient', Mock(return_value=client))
    send = Mock(return_value=True)
    monkeypatch.setattr('app.cli.send_new_task', send)
    post = Mock()
    monkeypatch.setattr('app.cli.httpx.post', post)
    assert main(['notify', 'recExample']) == 0
    send.assert_called_once_with('recExample', {'title': '标题'}, client)
    post.assert_not_called()
