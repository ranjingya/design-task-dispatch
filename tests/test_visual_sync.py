"""验证群消息预览、启动边界与登记后的提醒重试。"""

import json
import sqlite3

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from app.visual_sync import VisualSync


def message(worker, who, text):
    """构造带有真实接口结构的消息。"""
    return {"sender": {"sender_type": "user", "id": "designer"}, "msg_type": "text",
            "chat_id": "oc_test", "message_position": "0", "message_id": "sample", "create_time": "1730000000123",
            "mentions": [{"id": who, "key": "@_user_1", "name": "同事"}],
            "body": {"content": json.dumps({"text": text})}}


def worker_with_catalog():
    """构造只使用模拟飞书接口的处理器。"""
    client = Mock()
    client.settings.visual_sync_chat_id = "oc_test"
    worker = VisualSync(client)
    worker.members = {"甲": "a", "乙": "b"}
    worker.catalog = [{"fields": {"货号+品名": title, "负责人": owner}, "record_id": str(i)}
                      for i, (title, owner) in enumerate([
                          ("ABC1234 帽子", "甲"), ("ABC1234 帽子（青少年版）", "乙"),
                          ("ABC1234 XYZ9999 帽子+围巾", "乙")])]
    return worker


def test_preview_mentions_variants_and_partial_match():
    worker = worker_with_catalog()
    text = "@_user_1\nABC1234帽子（青少年） 主图修改\nXYZ0000手套 数据包已完成"
    for who in ("always_mention", "conditional_mention"):
        rows = worker.plan(message(worker, worker.config[who], text))
        assert len(rows) == 1
        assert rows[0]["负责人"] == [{"id": "b"}]
        assert rows[0]["品名"] == "帽子（青少年）"
        assert rows[0]["修改信息"] == text.replace("@_user_1", "@同事")
        assert rows[0]["原消息链接"] == {"text": "查看原消息", "link": "https://applink.feishu.cn/client/chat/open?openChatId=oc_test&position=0"}
        assert rows[0]["需求类型"] == "优化"
    assert not worker.plan(message(worker, "other", text))
    vague = "ABC1234帽子\nY:\\数据包"
    assert not worker.plan(message(worker, worker.config["conditional_mention"], vague))
    assert "需求类型" not in worker.plan(message(worker, worker.config["always_mention"], vague))[0]
    combo = "ABC1234 XYZ9999 帽子+围巾 数据包已完成"
    assert [r["货号"] for r in worker.plan(message(worker, worker.config["always_mention"], combo))] == ["ABC1234 XYZ9999"]
    worker.client.send_card.assert_not_called()
    worker.client.create_visual_record.assert_not_called()


def test_pagination_excludes_before_start_and_next_window():
    worker = worker_with_catalog()
    worker.client.material_get.side_effect = [
        {"items": [{"create_time": str(t)} for t in [1000, 1100]], "has_more": True, "page_token": "next"},
        {"items": [{"create_time": str(t)} for t in [1200, 1800]]}]
    assert [m["create_time"] for m in worker.messages(1100, 1800)] == ["1100", "1200"]
    assert all(call.args[1]["container_id"] == "oc_test" for call in worker.client.material_get.call_args_list)


@pytest.mark.parametrize("third_succeeds", [False, True])
def test_restart_retries_notification_without_second_record(tmp_path, monkeypatch, third_succeeds):
    worker = worker_with_catalog()
    worker.client.settings = SimpleNamespace(rules_cache_path=tmp_path / "rules.json", base_token="base", app_id="data")
    worker.client.create_visual_record.return_value = "record"
    worker.bot = Mock()
    worker.bot.settings.app_id = "alert"
    worker.client.get_contact_user.return_value = {"union_id": "on_test"}
    worker.bot.get_contact_user_by_union_id.return_value = {"open_id": "ou_alert"}
    error = RuntimeError("模拟发送失败")
    worker.bot.send_card.side_effect = [error, error, "om_ok" if third_succeeds else error]
    msg = message(worker, worker.config["always_mention"], "ABC1234帽子 数据包已完成")
    worker.messages = Mock(return_value=[msg, msg])
    row = worker.plan(msg)[0]
    worker.plan = Mock(return_value=[row])
    monkeypatch.setattr("app.visual_sync.time.time", lambda: now)
    for now, count in [(20, 1), (49, 1), (50, 2), (79, 2), (80, 3), (110, 3)]:
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        worker.poll(stop, 10000, Mock())
        assert worker.bot.send_card.call_count == count
    assert worker.messages.call_args_list[0].args == (10000, 17000)
    worker.client.create_visual_record.assert_called_once()
    calls = worker.bot.send_card.call_args_list
    assert calls[0] == calls[1] == calls[2]
    assert calls[0].args[0] == "ou_alert"
    assert calls[0].kwargs == {"retry_transient": False}
    worker.client.send_card.assert_not_called()
    with sqlite3.connect(tmp_path / "visual_sync.sqlite3") as db:
        assert db.execute("SELECT sent, notify_attempts FROM jobs").fetchall() == [(int(third_succeeds), 3)]


def test_server_event_only_wakes_for_target_group_users(monkeypatch):
    import lark_oapi as lark
    from app.visual_sync import listen_visual_events

    connection = Mock()
    monkeypatch.setattr(lark.ws, "Client", connection)
    received = Mock()
    settings = SimpleNamespace(app_id="app", app_secret="secret", visual_sync_chat_id="oc_test")
    listen_visual_events(settings, received)
    connection.return_value.start.assert_called_once()
    handler = connection.call_args.kwargs["event_handler"]
    for chat, sender in [("oc_other", "user"), ("oc_test", "app"), ("oc_test", "user")]:
        payload = {"schema": "2.0", "header": {"event_type": "im.message.receive_v1"},
                   "event": {"sender": {"sender_type": sender}, "message": {"chat_id": chat, "message_id": "om_test"}}}
        handler._do_without_validation(json.dumps(payload).encode())
    received.assert_called_once_with("om_test")
