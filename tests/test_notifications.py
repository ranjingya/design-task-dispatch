"""新增通知的时序、私聊接收人、持久化去重和补发验证。"""

import json
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings
from app.feishu import FeishuClient
from app.notifications import deadline_date, load_notifications, notification_content, notify_record, send_new_task

MAPPING = {key: key for key in ['title', 'description', 'submitter', 'department', 'platform',
                              'recommended_assignee', 'work_type', 'level', 'load_score',
                              'rule_explanation', 'recommendation_result']}


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings('data-app', 'data-secret', 'base', 'table', 'token', tmp_path / 'fields.json',
                        rules_cache_path=tmp_path / 'cache/rules.json',
                        alert_app_id='alert-app', alert_app_secret='alert-secret')
    (tmp_path / 'notifications.toml').write_text('''[new_task]
enabled = true
[[new_task.recipients]]
name = "接收人甲"
open_id = "ou_usera"
enabled = true
[[new_task.recipients]]
name = "接收人乙"
open_id = "ou_userb"
enabled = false
''')
    client = Mock(spec=FeishuClient)
    client.settings = settings
    client.get_record.return_value = {'title': '新需求', 'description': '需求正文',
        'recommended_assignee': [{'id': 'primary', 'name': '主负责人'}, {'id': 'backup', 'name': '副负责人'}],
        'load_score': 7, 'rule_explanation': '已匹配', 'recommendation_result': '对应人员'}
    return client


def test_only_enabled_person_and_persistent_dedup(client, monkeypatch):
    send = Mock(return_value='om_test')
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    assert send_new_task('recTest', MAPPING, client)
    assert send_new_task('recTest', MAPPING, client)
    assert send.call_count == 1
    assert send.call_args.args[0] == 'ou_usera'
    text = json.dumps(send.call_args.args[1], ensure_ascii=False)
    assert '主负责人 副负责人' in text
    assert '任务负载分' not in text
    assert send.call_args.args[1]['header']['template'] == 'purple'
    assert 'record=recTest' in text
    assert client.get_record.call_count == 1
    states = list((client.settings.rules_cache_path.parent / 'notifications').glob('*.json'))
    assert len(states) == 1 and json.loads(states[0].read_text())['sent']
    assert states[0].stat().st_mode & 0o777 == 0o600


def test_partial_failure_only_retries_failed_person(client, monkeypatch):
    path = client.settings.fields_path.parent / 'notifications.toml'
    path.write_text(path.read_text().replace('enabled = false', 'enabled = true'))
    send = Mock(side_effect=['om_a', RuntimeError('暂时失败'), 'om_b'])
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    assert not send_new_task('recTest', MAPPING, client)
    assert send_new_task('recTest', MAPPING, client)
    assert [call.args[0] for call in send.call_args_list] == ['ou_usera', 'ou_userb', 'ou_userb']
    assert send.call_args_list[1].args[2] == send.call_args_list[2].args[2]


def test_manual_assignment_is_explicit(client):
    client.get_record.return_value['recommended_assignee'] = []
    result = notification_content('recTest', client.get_record.return_value, MAPPING, client.settings, client)
    assert '待人工分配' in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize('change', [{'recommendation_result': ''}, {'rule_explanation': '处理失败：请求失败'},
                                   {'recommended_assignee': [{'id': 'no-name'}]}])
def test_incomplete_results_never_send(client, monkeypatch, change):
    client.get_record.return_value.update(change)
    send = Mock()
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    assert not send_new_task('recTest', MAPPING, client)
    send.assert_not_called()


def test_unknown_delivery_outside_dedup_window_does_not_resend(client, monkeypatch):
    send = Mock(side_effect=RuntimeError('网络失败'))
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    assert not send_new_task('recTest', MAPPING, client)
    path = next((client.settings.rules_cache_path.parent / 'notifications').glob('*.json'))
    state = json.loads(path.read_text())
    state['started_at'] = 0
    path.write_text(json.dumps(state))
    assert not send_new_task('recTest', MAPPING, client)
    assert send.call_count == 1


def test_disabled_config_never_reads_record(client, monkeypatch):
    path = client.settings.fields_path.parent / 'notifications.toml'
    path.write_text('[new_task]\nenabled = false\n')
    assert send_new_task('recTest', MAPPING, client)
    client.get_record.assert_not_called()


def test_invalid_boolean_is_rejected(client):
    path = client.settings.fields_path.parent / 'notifications.toml'
    path.write_text('[new_task]\nenabled = "false"\n')
    with pytest.raises(ValueError):
        load_notifications(path)


def test_message_uses_alert_credentials_and_same_uuid_on_retry(client, monkeypatch):
    bot = FeishuClient(replace(client.settings, app_id='alert-app', app_secret='alert-secret'))
    request = Mock(side_effect=[
        httpx.Response(200, json={'code': 0, 'tenant_access_token': 'alert-token', 'expire': 7200}),
        httpx.ReadTimeout('secret'),
        httpx.Response(200, json={'code': 0, 'data': {'message_id': 'om_ok'}}),
    ])
    monkeypatch.setattr('app.feishu.httpx.request', request)
    monkeypatch.setattr('app.feishu.time.sleep', Mock())
    assert bot.send_card('ou_usera', {'zh_cn': {}}, 'stable-uuid') == 'om_ok'
    auth, first, retry = request.call_args_list
    assert auth.kwargs['json']['app_id'] == 'alert-app'
    assert first.kwargs['headers']['Authorization'] == 'Bearer alert-token'
    assert first.kwargs['json'] == retry.kwargs['json']
    assert first.kwargs['params'] == {'receive_id_type': 'open_id'}


def test_card_mentions_use_alert_open_ids_and_escape_input(client, monkeypatch):
    client.get_contact_user.side_effect = lambda uid: {'union_id': 'on_' + uid}
    lookup = Mock(side_effect=lambda uid: {'open_id': 'ou_alert' + uid[3:]})
    monkeypatch.setattr(FeishuClient, 'get_contact_user_by_union_id', lookup)
    fields = client.get_record.return_value | {'description': '<at id=all></at> **业务文本**'}
    result = notification_content('recTest', fields, MAPPING, client.settings, client)
    body = result['body']['elements'][0]['content']
    assert '<at id="ou_alertprimary"></at> <at id="ou_alertbackup"></at>' in body
    assert '<at id=all>' not in body
    assert '&lt;at id=all&gt;' in body
    assert result['body']['elements'][1]['behaviors'][0]['type'] == 'open_url'


@pytest.mark.parametrize('invalid', ['user_a', '75d3978a', 'ou_', 'ou_<invalid>'])
def test_recipient_requires_open_id(client, invalid):
    path = client.settings.fields_path.parent / 'notifications.toml'
    path.write_text(path.read_text().replace('ou_usera', invalid))
    with pytest.raises(ValueError, match='open_id'):
        load_notifications(path)


def test_same_app_card_uses_original_open_id(client):
    settings = replace(client.settings, alert_app_id=client.settings.app_id)
    fields = client.get_record.return_value | {'recommended_assignee': [{'id': 'ou_primary', 'name': '主负责人'}]}
    card = notification_content('recTest', fields, MAPPING, settings, client)
    assert '<at id="ou_primary"></at>' in card['body']['elements'][0]['content']
    client.get_contact_user.assert_not_called()


@pytest.mark.parametrize('event,expected', [('leader_confirmed', ['ou_first', 'ou_second']),
                                         ('assignee_confirmed', ['ou_creator'])])
def test_confirmation_recipients_and_event_dedup(client, monkeypatch, event, expected):
    from app.notifications import notify_record
    client.settings = replace(client.settings, alert_app_id=client.settings.app_id)
    mapping = MAPPING | {k: k for k in ['assignee', 'creator', 'leader_confirmed', 'assignee_confirmed', 'cancelled', 'completed_at']}
    path = client.settings.fields_path.parent / 'notifications.toml'
    path.write_text(path.read_text() + '\n[leader_confirmed]\nenabled=true\nrecipient_field="assignee"\n[assignee_confirmed]\nenabled=true\nrecipient_field="creator"\n')
    fields = client.get_record.return_value
    fields.update({'assignee': [{'id': 'ou_first', 'name': '甲'}, {'id': 'ou_second', 'name': '乙'}],
                   'creator': {'id': 'ou_creator', 'name': '创建人'},
                   'submitter': [{'id': 'ou_other', 'name': '需求人'}],
                   'leader_confirmed': True, 'assignee_confirmed': True})
    fields.pop('rule_explanation')
    fields.pop('recommendation_result')
    send = Mock(return_value='om_ok')
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    assert notify_record('recConfirm', event, mapping, client) == 'sent_or_duplicate'
    assert notify_record('recConfirm', event, mapping, client) == 'sent_or_duplicate'
    assert [c.args[0] for c in send.call_args_list] == expected
    card = send.call_args.args[1]
    assert len(card['body']['elements']) == 2
    assert '最终负责人' in card['body']['elements'][0]['content']
    assert '推荐负责人' not in card['body']['elements'][0]['content']
    fields[event] = False
    assert notify_record('recConfirm', event, mapping, client) == 'skipped'
    fields[event] = True
    fields['assignee'] = [{'id': 'ou_third', 'name': '丙'}]
    assert notify_record('recConfirm', event, mapping, client) == 'sent_or_duplicate'
    assert send.call_count == len(expected) + 1
    fields['cancelled'] = True
    assert notify_record('recConfirm', event, mapping, client) == 'skipped'
    client.update_record.assert_not_called()


@pytest.fixture
def deadline_client(client, monkeypatch):
    """构造当天截止、多人负责的独立通知场景。"""
    monkeypatch.setenv('TZ', 'Asia/Shanghai')
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 19, 9, tzinfo=ZoneInfo('Asia/Shanghai'))
    monkeypatch.setattr('app.notifications.datetime', clock)
    client.settings = replace(client.settings, alert_app_id=client.settings.app_id)
    mapping = MAPPING | {k: k for k in ['assignee', 'creator', 'deadline', 'cancelled', 'completed_at']}
    path = client.settings.fields_path.parent / 'notifications.toml'
    path.write_text(path.read_text() + '\n[deadline_reminder]\nenabled=true\nrecipient_field="assignee"\n')
    client.get_record.return_value = {
        'title': '到期需求', 'platform': '测试平台',
        'submitter': [{'id': 'ou_requester', 'name': '提报人'}],
        'assignee': [{'id': 'ou_first', 'name': '甲'}, {'id': 'ou_second', 'name': '乙'},
                     {'id': 'ou_first', 'name': '甲'}],
        'deadline': int(clock.now.return_value.replace(hour=0).timestamp() * 1000),
    }
    return client, mapping, clock


def test_deadline_sends_to_all_current_owners_once(deadline_client, monkeypatch):
    """按最终负责人逐人发送，重复人员和重复请求均不重复通知。"""
    client, mapping, _ = deadline_client
    send = Mock(return_value='om_due')
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    for _ in range(2):
        assert notify_record('recDue', 'deadline_reminder', mapping, client) == 'sent_or_duplicate'
    assert [call.args[0] for call in send.call_args_list] == ['ou_first', 'ou_second']
    card = send.call_args.args[1]
    assert card['header'] == {'title': {'tag': 'plain_text', 'content': '美工任务 · 截止日期提醒'},
                              'template': 'red'}
    body = card['body']['elements'][0]['content']
    assert body.startswith('**任务即将到期**\n\n')
    assert '任务名称' in body and '到期需求' in body and '需求人员' in body and '所属平台' in body
    assert '<at id="ou_requester"></at>' in body
    assert '推荐负责人' not in body
    assert 'record=recDue' in card['body']['elements'][1]['behaviors'][0]['default_url']
    client.update_record.assert_not_called()


@pytest.mark.parametrize('change', [
    {'assignee': []}, {'cancelled': True}, {'cancelled': 1}, {'completed_at': 1792339200000},
    {'deadline': None}, {'deadline': ''}, {'deadline': 1792252800000}, {'deadline': 1792425600000},
])
def test_deadline_skips_ineligible_latest_record(deadline_client, monkeypatch, change):
    """最新记录已结束、无人负责或截止日不是今天时不发送。"""
    client, mapping, _ = deadline_client
    client.get_record.return_value.update(change)
    send = Mock()
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    assert notify_record('recDue', 'deadline_reminder', mapping, client) == 'skipped'
    send.assert_not_called()


def test_deadline_new_owner_does_not_repeat_existing_owner(deadline_client, monkeypatch):
    """同一截止日增换负责人时，只通知尚未收到提醒的人员。"""
    client, mapping, _ = deadline_client
    send = Mock(return_value='om_due')
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    notify_record('recDue', 'deadline_reminder', mapping, client)
    client.get_record.return_value['assignee'] = [
        {'id': 'ou_second', 'name': '乙'}, {'id': 'ou_third', 'name': '丙'}]
    notify_record('recDue', 'deadline_reminder', mapping, client)
    client.get_record.return_value['assignee'].reverse()
    notify_record('recDue', 'deadline_reminder', mapping, client)
    assert [call.args[0] for call in send.call_args_list] == ['ou_first', 'ou_second', 'ou_third']


def test_deadline_reschedule_allows_new_date_reminder(deadline_client, monkeypatch):
    """调整到新的截止日后可以再次提醒，同日修改时间不重复提醒。"""
    client, mapping, clock = deadline_client
    send = Mock(return_value='om_due')
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    notify_record('recDue', 'deadline_reminder', mapping, client)
    client.get_record.return_value['deadline'] += 3600 * 1000
    notify_record('recDue', 'deadline_reminder', mapping, client)
    assert send.call_count == 2
    client.get_record.return_value['deadline'] += 24 * 3600 * 1000
    assert notify_record('recDue', 'deadline_reminder', mapping, client) == 'skipped'
    clock.now.return_value = clock.now.return_value.replace(day=20)
    notify_record('recDue', 'deadline_reminder', mapping, client)
    assert send.call_count == 4
    assert send.call_args_list[0].args[2] != send.call_args_list[2].args[2]


def test_deadline_partial_failure_retries_only_failed_owner(deadline_client, monkeypatch):
    """部分人员发送失败后，相同请求只补发失败人员，保留消息 UUID。"""
    client, mapping, _ = deadline_client
    send = Mock(side_effect=['om_first', RuntimeError('模拟失败'), 'om_second'])
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    with pytest.raises(RuntimeError, match='可重试'):
        notify_record('recDue', 'deadline_reminder', mapping, client)
    assert notify_record('recDue', 'deadline_reminder', mapping, client) == 'sent_or_duplicate'
    assert [call.args[0] for call in send.call_args_list] == ['ou_first', 'ou_second', 'ou_second']
    assert send.call_args_list[1].args[2] == send.call_args_list[2].args[2]


def test_disabled_deadline_does_not_read_record(deadline_client):
    """关闭提醒配置时直接返回关闭状态。"""
    client, mapping, _ = deadline_client
    path = client.settings.fields_path.parent / 'notifications.toml'
    path.write_text('[deadline_reminder]\nenabled=false\n')
    assert notify_record('recDue', 'deadline_reminder', mapping, client) == 'disabled'
    client.get_record.assert_not_called()


def test_deadline_recipient_field_must_be_final_owner(deadline_client, monkeypatch):
    """截止提醒只支持最终负责人字段，错误配置不得发送给其他人。"""
    client, mapping, _ = deadline_client
    path = client.settings.fields_path.parent / 'notifications.toml'
    path.write_text('[deadline_reminder]\nenabled=true\nrecipient_field="creator"\n')
    send = Mock()
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    with pytest.raises(ValueError, match='assignee'):
        notify_record('recDue', 'deadline_reminder', mapping, client)
    send.assert_not_called()


def test_deadline_converts_each_owner_to_alert_open_id(deadline_client, monkeypatch):
    """数据应用和通知应用不同时，通过 Union ID 转换每位收件人。"""
    client, mapping, _ = deadline_client
    client.settings = replace(client.settings, alert_app_id='different-alert-app')
    client.get_contact_user.side_effect = lambda uid: {'union_id': 'on_' + uid}
    monkeypatch.setattr(FeishuClient, 'get_contact_user_by_union_id',
                        lambda self, uid: {'open_id': 'ou_alert' + uid.removeprefix('on_ou_')})
    send = Mock(return_value='om_due')
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    notify_record('recDue', 'deadline_reminder', mapping, client)
    assert [call.args[0] for call in send.call_args_list] == ['ou_alertfirst', 'ou_alertsecond']


@pytest.mark.parametrize('value', [True, '日期无效', float('nan'), float('inf')])
def test_invalid_deadline_is_retryable_error(deadline_client, monkeypatch, value):
    """无效截止时间抛出异常，不产生提醒或错误去重记录。"""
    client, mapping, _ = deadline_client
    client.get_record.return_value['deadline'] = value
    send = Mock()
    monkeypatch.setattr(FeishuClient, 'send_card', send)
    with pytest.raises(ValueError, match='毫秒时间戳'):
        notify_record('recDue', 'deadline_reminder', mapping, client)
    send.assert_not_called()


def test_deadline_uses_service_timezone_for_timestamp(deadline_client):
    """UTC 的前一日晚间属于服务时区的下一自然日，数字字符串也可读取。"""
    _, _, clock = deadline_client
    value = int(datetime.fromisoformat('2026-10-18T16:00:00+00:00').timestamp() * 1000)
    assert deadline_date(str(value)) == clock.now.return_value.date()
