"""验证群消息预览、启动边界与登记后的提醒重试。"""

import json
import sqlite3

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from app.visual_sync import VisualSync, message_text


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


def preview(worker, msg):
    """用独立内存库预览一条消息的登记字段。"""
    with sqlite3.connect(":memory:") as db:
        worker.prepare_jobs(db)
        return worker.queue_message(db, msg)


def test_preview_mentions_variants_and_partial_match():
    worker = worker_with_catalog()
    text = "@_user_1\nABC1234帽子（青少年） 主图修改\nXYZ0000手套 数据包已完成"
    for who in ("always_mention", "conditional_mention"):
        rows = preview(worker, message(worker, worker.config[who], text))
        assert len(rows) == 1
        assert rows[0]["负责人"] == [{"id": "b"}]
        assert rows[0]["品名"] == "帽子（青少年）"
        assert rows[0]["修改信息"] == text.replace("@_user_1", "@同事")
        assert rows[0]["原消息链接"] == {"text": "查看原消息", "link": "https://applink.feishu.cn/client/chat/open?openChatId=oc_test&position=0"}
        assert rows[0]["需求类型"] == "优化"
    assert not preview(worker, message(worker, "other", text))
    vague = "ABC1234帽子\nY:\\数据包"
    assert not preview(worker, message(worker, worker.config["conditional_mention"], vague))
    assert "需求类型" not in preview(worker, message(worker, worker.config["always_mention"], vague))[0]
    combo = "ABC1234 XYZ9999 帽子+围巾 数据包已完成"
    assert [r["货号"] for r in preview(worker, message(worker, worker.config["always_mention"], combo))] == ["ABC1234 XYZ9999"]
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
    report = Mock()
    monkeypatch.setattr("app.visual_sync.send_error", report)
    error = RuntimeError("模拟发送失败")
    worker.bot.send_card.side_effect = [error, error, "om_ok" if third_succeeds else error]
    msg = message(worker, worker.config["always_mention"], "ABC1234帽子 数据包已完成")
    worker.messages = Mock(return_value=[msg, msg])
    worker.plan = Mock(return_value=worker.plan(msg))
    monkeypatch.setattr("app.visual_sync.time.time", lambda: now)
    for now, count in [(20, 1), (49, 1), (50, 2), (79, 2), (80, 3), (110, 3)]:
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        worker.poll(stop, 10000, Mock())
        assert worker.bot.send_card.call_count == count
        assert report.call_args.args[:2] == ("record", "发送负责人提醒")
    assert worker.messages.call_args_list[0].args == (10000, 17000)
    worker.client.create_visual_record.assert_called_once()
    calls = worker.bot.send_card.call_args_list
    assert calls[0] == calls[1] == calls[2]
    assert calls[0].args[0] == "ou_alert"
    assert calls[0].kwargs == {"retry_transient": False}
    content, buttons = calls[0].args[1]["body"]["elements"]
    assert '**负责人：** <at id="ou_alert"></at>' in content["content"]
    assert '**视觉设计：** <at id="ou_alert"></at>' in content["content"]
    assert "同步平台" not in content["content"]
    assert "**修改内容：**\nABC1234帽子 数据包已完成" in content["content"]
    assert buttons["tag"] == "column_set" and buttons["flex_mode"] == "none"
    assert [c["elements"][0]["text"]["content"] for c in buttons["columns"]] == ["查看原消息", "查看需求"]
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

def test_notification_body_keeps_paths_links_and_plain_at_characters():
    raw = {"title": "素材", "content": [
        [{"tag": "img", "image_key": "sample"}],
        [{"tag": "text", "text": "ABC1234 帽子 主图更新"}],
        [{"tag": "text", "text": r"Y:\设计\帽子"}, {"tag": "at", "user_id": "@_user_1"},
         {"tag": "text", "text": " 请发到 art@example.com"}],
        [{"tag": "a", "text": "脚本", "href": "https://example.com/script"}],
    ]}
    msg = {"msg_type": "post", "body": {"content": json.dumps(raw)},
           "mentions": [{"key": "@_user_1", "name": "设计"}]}
    text = message_text(msg, include_mentions=False)
    assert "ABC1234 帽子 主图更新" in text
    assert r"Y:\设计\帽子 请发到 art@example.com" in text
    assert "脚本 https://example.com/script" in text
    assert "@设计" not in text and "sample" not in text


def test_message_images_keep_order_and_reuse_successful_uploads():
    worker = worker_with_catalog()
    body = {"content": [[{"tag": "img", "image_key": key}] for key in ("one", "two", "one")]}
    body["content_v2"] = body["content"]
    msg = {"message_id": "om_source", "msg_type": "post", "body": {"content": json.dumps(body)}}
    worker.client.upload_message_image.side_effect = ["first", RuntimeError("模拟上传失败"), "second"]
    with sqlite3.connect(":memory:") as db:
        worker.prepare_jobs(db)
        worker.register_images(db, msg)
        db.commit()
        with pytest.raises(RuntimeError, match="模拟上传失败"):
            worker.attach_images(db, "om_source", {})
        for _ in range(2):
            row = {}
            worker.attach_images(db, "om_source", row)
            assert row["消息附件"] == [{"file_token": "first"}, {"file_token": "second"}]
    assert [call.args[1] for call in worker.client.upload_message_image.call_args_list] == ["one", "two", "two"]


def test_unavailable_owner_transfers_once_and_retries_only_forward(tmp_path, monkeypatch):
    """负责人不可见后立刻转交。转交失败先报错，重开状态库仍只重试接手人。"""
    from app.feishu import FeishuAPIError

    worker = worker_with_catalog()
    worker.client.settings = SimpleNamespace(fields_path=tmp_path / "fields.json", base_token="base")
    (tmp_path / "notifications.toml").write_text(
        '[visual_owner_unavailable]\nenabled=true\n[[visual_owner_unavailable.recipients]]\n'
        'name="接手人"\nopen_id="ou_backup"\nenabled=true\n')
    monkeypatch.setattr("app.visual_sync.alert_open_id", lambda uid, *_: f"ou_{uid}")
    error = Mock()
    monkeypatch.setattr("app.visual_sync.send_error", error)
    unavailable = FeishuAPIError("机器人不可见", "/im/v1/messages", 230013)
    worker.bot = Mock()
    worker.bot.send_card.side_effect = [unavailable, unavailable, "om_ok"]
    msg = message(worker, worker.config["always_mention"], "ABC1234 帽子 数据包已完成")
    row = preview(worker, msg)[0]
    path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(path) as db:
        worker.prepare_jobs(db)
        db.execute("INSERT INTO jobs(id, payload, record, notification_body) VALUES(?,?,?,?)",
                   ("job", json.dumps(row), "rec", "原文"))
        worker.deliver(db)
    error.assert_called_once()
    assert error.call_args.args[:2] == ("rec", "转交负责人提醒")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET notify_after=0")
        worker.deliver(db)
        worker.deliver(db)
        assert db.execute("SELECT sent, unavailable_owner FROM jobs").fetchone() == (1, "ou_a")
    original, forward, retry = worker.bot.send_card.call_args_list
    assert original.args[0] == "ou_a" and forward.args[0] == "ou_backup"
    assert original.args[2] != forward.args[2] and forward == retry
    card = forward.args[1]
    assert card["header"]["template"] == "red"
    panel, = card["body"]["elements"]
    assert panel["background_style"] == "red-50"
    markdown, buttons = panel["columns"][0]["elements"]
    assert '**负责人：** <at id="ou_a"></at>（目标人员已不再可见）' in markdown["content"]
    assert '**视觉设计：** <at id="ou_designer"></at>' in markdown["content"]
    assert len(buttons["columns"]) == 2
    worker.client.create_visual_record.assert_not_called()


def test_program_error_in_message_parsing_alerts_immediately(tmp_path, monkeypatch):
    """解析异常直接报告并保留游标。"""
    worker = worker_with_catalog()
    worker.client.settings.rules_cache_path = tmp_path / "rules.json"
    worker.messages = Mock(return_value=[{}])
    stop = Mock()
    stop.is_set.side_effect = [False, True]
    report = Mock()
    monkeypatch.setattr("app.visual_sync.send_error", report)
    worker.poll(stop, 10000, Mock())
    report.assert_called_once()
    assert report.call_args.args[:2] == ("", "读取与解析群消息")
    assert isinstance(report.call_args.args[2], KeyError)
    with sqlite3.connect(tmp_path / "visual_sync.sqlite3") as db:
        assert db.execute("SELECT cursor FROM progress").fetchone() == (10000,)


def test_send_api_preserves_unavailable_error_code(monkeypatch):
    """从飞书原始业务响应提取收件人不可见错误码。"""
    import httpx
    from app.feishu import FeishuAPIError, FeishuClient

    monkeypatch.setattr(httpx, "request", Mock(return_value=httpx.Response(400, json={"code": 230013})))
    with pytest.raises(FeishuAPIError) as failure:
        FeishuClient._request("POST", "/im/v1/messages", retry_transient=False)
    assert (failure.value.path, failure.value.code) == ("/im/v1/messages", 230013)


def test_visual_reassignment_preserves_order_on_replay_rollback_and_restart(tmp_path, monkeypatch):
    """青提按款轮换，花菜固定交接；回滚、重复投递或重新开库不跳号。"""
    worker = VisualSync(Mock(settings=SimpleNamespace(visual_sync_chat_id="oc_test", base_token="base")))
    worker.catalog = [
        {"record_id": str(i), "fields": {"货号+品名": code + " 帽子", "负责人": name}}
        for i, (code, name) in enumerate([("KQ12345", "青提"), ("KQ12346", "花菜"), ("KQ12347", "饭团")])]
    worker.bot = Mock()
    worker.client.create_visual_record.return_value = "rec"
    monkeypatch.setattr("app.visual_sync.alert_open_id", lambda uid, *_: uid)
    path = tmp_path / "jobs.db"
    order = ["塔可", "软糖", "饭团", "饭团", "蛋糕", "软糖", "塔可"]
    for i, code in enumerate(["KQ12345", "KQ12346", "KQ12347"] + ["KQ12345"] * 4):
        msg = message(worker, worker.config["always_mention"], code + "帽子 主图修改")
        msg["message_id"] = str(i)
        expected = worker.members[order[i]]
        with sqlite3.connect(path) as db:
            worker.prepare_jobs(db)
            rows = worker.queue_message(db, msg)
            db.rollback()
            assert rows == worker.queue_message(db, msg)
            assert rows[0]["负责人"] == [{"id": expected}]
            assert worker.queue_message(db, msg) == []
            db.commit()
            worker.deliver(db)
            assert worker.client.create_visual_record.call_args.args[1]["负责人"] == [{"id": expected}]
            assert worker.bot.send_card.call_args.args[0] == expected
    assert worker.bot.send_card.call_count == len(order)
