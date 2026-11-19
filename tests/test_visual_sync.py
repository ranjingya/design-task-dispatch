"""验证群消息预览、启动边界与登记后的提醒重试。"""

import asyncio
import json
import sqlite3

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


@pytest.mark.parametrize("fifth_succeeds", [False, True])
def test_restart_retries_notification_without_second_record(tmp_path, monkeypatch, fifth_succeeds):
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
    worker.bot.send_card.side_effect = [error] * 4 + ["om_ok" if fifth_succeeds else error]
    msg = message(worker, worker.config["always_mention"], "ABC1234帽子 数据包已完成")
    worker.messages = Mock(return_value=[msg, msg])
    worker.plan = Mock(return_value=worker.plan(msg))
    monkeypatch.setattr("app.visual_sync.time.time", lambda: now)
    for now, count in [(20, 1), (49, 1), (50, 2), (109, 2), (110, 3), (229, 3), (230, 4), (469, 4), (470, 5), (800, 5)]:
        stop = Mock()
        stop.is_set.side_effect = [False, False, True] if now == 20 else [False, True]
        wake = Mock()
        wake.wait.side_effect = [True, False] if now == 20 else [False]
        worker.poll(stop, 10000, wake)
        assert worker.messages.call_count == 1
        if now >= 470:
            wake.wait.assert_called_once_with(None)
        assert worker.bot.send_card.call_count == count
        assert report.call_args.args[:2] == ("record", "发送负责人提醒")
    assert worker.messages.call_args_list[0].args == (10000, 17000)
    worker.client.create_visual_record.assert_called_once()
    calls = worker.bot.send_card.call_args_list
    assert calls == [calls[0]] * 5
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
        assert db.execute("SELECT sent, notify_attempts, register_attempts FROM jobs").fetchall() == [(int(fifth_succeeds), 5, 1)]


def test_server_event_only_wakes_for_target_group_users(monkeypatch):
    import lark_oapi as lark
    from app.visual_sync import listen_visual_events

    def init_client(client, *_args, event_handler, **_kwargs):
        """保留真实的事件分发器，隔离 SDK 后台缓存和网络连接。"""
        client._event_handler = event_handler

    instances = []
    monkeypatch.setattr(lark.ws.Client, "__init__", init_client)
    connect = AsyncMock()
    monkeypatch.setattr(lark.ws.Client, "_connect", connect)
    monkeypatch.setattr(lark.ws.Client, "start", lambda self: instances.append(self))
    received, connected = Mock(), Mock()
    settings = SimpleNamespace(app_id="app", app_secret="secret", visual_sync_chat_id="oc_test")
    listen_visual_events(settings, received, connected)
    client, = instances
    connected.assert_not_called()
    for _ in range(2):
        asyncio.run(client._connect())
    assert connected.call_count == connect.await_count == 2
    handler = client._event_handler
    for chat, sender in [("oc_other", "user"), ("oc_test", "app"), ("oc_test", "user")]:
        payload = {"schema": "2.0", "header": {"event_type": "im.message.receive_v1"},
                   "event": {"sender": {"sender_type": sender}, "message": {"chat_id": chat, "message_id": "om_test"}}}
        handler._do_without_validation(json.dumps(payload).encode())
    for kind in ("message_read", "reaction.created", "reaction.deleted"):
        payload = {"schema": "2.0", "header": {"event_type": f"im.message.{kind}_v1"}, "event": {}}
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


def test_unavailable_owner_alerts_without_forwarding(tmp_path, monkeypatch):
    """旧任务带有转发标记时，也只向实际负责人发送并按普通错误告警。"""
    from app.feishu import FeishuAPIError

    worker = worker_with_catalog()
    worker.client.settings = SimpleNamespace(base_token="base")
    monkeypatch.setattr("app.visual_sync.alert_open_id", lambda uid, *_: f"ou_{uid}")
    report = Mock()
    monkeypatch.setattr("app.visual_sync.send_error", report)
    worker.bot = Mock()
    worker.bot.send_card.side_effect = FeishuAPIError("负责人不可见", "/im/v1/messages", 230013)
    msg = message(worker, worker.config["always_mention"], "ABC1234 帽子 数据包已完成")
    with sqlite3.connect(":memory:") as db:
        worker.prepare_jobs(db)
        worker.queue_message(db, msg)
        db.execute("ALTER TABLE jobs ADD COLUMN unavailable_owner TEXT")
        db.execute("UPDATE jobs SET record='rec', unavailable_owner='ou_old'")
        worker.deliver(db)
        assert db.execute("SELECT sent, notify_attempts FROM jobs").fetchone() == (0, 1)
    worker.bot.send_card.assert_called_once()
    assert worker.bot.send_card.call_args.args[0] == "ou_a"
    report.assert_called_once()
    assert report.call_args.args[:2] == ("rec", "发送负责人提醒")


def test_program_error_in_message_parsing_alerts_immediately(tmp_path, monkeypatch):
    """解析异常直接报告并保留游标。"""
    worker = worker_with_catalog()
    worker.client.settings.rules_cache_path = tmp_path / "rules.json"
    worker.messages = Mock(return_value=[{}])
    stop = Mock()
    stop.is_set.side_effect = [False, False, True]
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


@pytest.mark.parametrize("fifth_succeeds", [False, True])
def test_failed_read_retries_to_cap_and_resumes_idle(tmp_path, monkeypatch, caplog, fifth_succeeds):
    """没有新事件时同一窗口反复失败也能恢复，只告警一次，恢复后继续等待事件。"""
    worker = worker_with_catalog()
    worker.client.settings.rules_cache_path = tmp_path / "rules.json"
    error = RuntimeError("模拟读取失败")
    worker.messages = Mock(side_effect=[error] * 4 + [[] if fifth_succeeds else error, []])
    alert = Mock()
    monkeypatch.setattr("app.visual_sync.send_error", alert)
    now = 20
    monkeypatch.setattr("app.visual_sync.time.time", lambda: now)
    stop = Mock()
    stop.is_set.return_value = False
    waits = []

    def wait(timeout):
        """首次握手后只推进虚拟时钟，恢复到空闲时结束。"""
        nonlocal now
        waits.append(timeout)
        if len(waits) == 1:
            return True
        if timeout is None:
            stop.is_set.return_value = True
        else:
            now += timeout
        return False

    wake = Mock()
    wake.wait.side_effect = wait
    caplog.set_level("INFO", logger="app.visual_sync")
    worker.poll(stop, 10000, wake)
    assert waits == [None, 30, 60, 120, 240, None]
    assert [call.args[0] for call in worker.messages.call_args_list] == [10000] * 5
    alert.assert_called_once()
    assert caplog.text.count("[优化] 群消息读取失败") == 1
    assert caplog.text.count("[优化] 群消息补读已恢复") == int(fifth_succeeds)
    with sqlite3.connect(tmp_path / "visual_sync.sqlite3") as db:
        assert db.execute("SELECT cursor FROM progress").fetchone() == ((now * 1000 - 3000 if fifth_succeeds else 10000),)
    if not fifth_succeeds:
        stop.is_set.side_effect = [False, False, True]
        wake.wait.side_effect = [True, False]
        worker.poll(stop, 10000, wake)
        assert worker.messages.call_count == 6
        assert worker.messages.call_args.args[0] == 10000


@pytest.mark.parametrize("fifth_succeeds", [False, True])
def test_registration_retry_limit_and_separate_notification_budget(tmp_path, monkeypatch, fifth_succeeds):
    """登记按时重试并跨重启保留次数，成功后提醒有独立的尝试次数。"""
    worker = worker_with_catalog()
    worker.bot = Mock()
    error = RuntimeError("模拟故障")
    worker.client.create_visual_record.side_effect = [error] * 4 + ["rec" if fifth_succeeds else error]
    worker.bot.send_card.side_effect = [error, "om_ok"]
    monkeypatch.setattr("app.visual_sync.send_error", Mock())
    monkeypatch.setattr("app.visual_sync.alert_open_id", lambda uid, *_: uid)
    monkeypatch.setattr("app.visual_sync.time.time", lambda: now)
    msg = message(worker, worker.config["always_mention"], "ABC1234帽子 数据包已完成")
    path = tmp_path / "jobs.sqlite3"
    for now, registrations in [(20, 1), (49, 1), (50, 2), (109, 2), (110, 3), (229, 3), (230, 4),
                               (469, 4), (470, 5), (499, 5), (500, 5), (800, 5)]:
        with sqlite3.connect(path) as db:
            worker.prepare_jobs(db)
            if now == 20:
                worker.queue_message(db, msg)
            worker.deliver(db)
            assert worker.client.create_visual_record.call_count == registrations
            notifications = int(fifth_succeeds) * (int(now >= 470) + int(now >= 500))
            assert worker.bot.send_card.call_count == notifications
            assert db.execute("SELECT register_attempts, notify_attempts FROM jobs").fetchone() == (registrations, notifications)


def test_restart_preserves_read_deadline_despite_connection_event(tmp_path, monkeypatch):
    """重启握手沿用未到期的补读时间，到点后从原游标继续。"""
    worker = worker_with_catalog()
    worker.client.settings.rules_cache_path = tmp_path / "rules.json"
    worker.messages = Mock(side_effect=[RuntimeError("读取失败"), []])
    monkeypatch.setattr("app.visual_sync.send_error", Mock())
    monkeypatch.setattr("app.visual_sync.time.time", lambda: now)
    for now, first_wait, calls in [(20, None, 1), (40, 10, 1), (50, None, 2)]:
        stop, wake = Mock(), Mock()
        stop.is_set.side_effect = [False, False, True] if now < 50 else [False, True]
        wake.wait.side_effect = [True, False] if now < 50 else [False]
        worker.poll(stop, 10000, wake)
        assert wake.wait.call_args_list[0].args == (first_wait,)
        assert worker.messages.call_count == calls
    assert worker.messages.call_args.args == (10000, 47000)


def test_migration_preserves_pending_and_finished_notifications():
    """旧通知沿用累计次数，升级和再次启动均保留已终止状态。"""
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT UNIQUE, payload TEXT, record TEXT, "
                   "sent INTEGER DEFAULT 0, notify_attempts INTEGER DEFAULT 0, notify_after REAL DEFAULT 0)")
        db.executemany("INSERT INTO jobs (id, record, notify_attempts, notify_after) VALUES (?, 'rec', ?, 100)",
                       [(str(n), n) for n in (1, 2, 3)])
        for _ in range(2):
            VisualSync.prepare_jobs(db)
            assert db.execute("SELECT notify_attempts, retry_at FROM jobs ORDER BY id").fetchall() == [
                (1, 100), (2, 130), (3, None)]
