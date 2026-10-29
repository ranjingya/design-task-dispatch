"""人员明细拆分、对账和后台隔离测试。"""

from copy import deepcopy
import json
import threading
from unittest.mock import Mock

import pytest

from app.config import Settings
from app.feishu import FeishuClient
from app.task_details import DETAIL_TYPES, SOURCE_TYPES, TaskDetailsSync, load_config, sync_details


@pytest.fixture
def client(tmp_path):
    """提供隔离的配置、源任务和目标记录。"""
    source = {key: key for key in SOURCE_TYPES} | {"description": "description"}
    detail = {key: key for key in DETAIL_TYPES}
    path = tmp_path / "fields.json"
    path.write_text(json.dumps(source))
    (tmp_path / "task_details.json").write_text(json.dumps({"interval_seconds": 60, "fields": detail}))
    client = Mock(spec=FeishuClient)
    client.settings = Settings("app", "secret", "base", "tasks", "token", path,
                               task_details_table_id="details")
    client.get_fields.side_effect = lambda table=None: [
        {"field_name": k, "type": v} for k, v in (DETAIL_TYPES if table else SOURCE_TYPES).items()]
    client.source = [{"record_id": "recOne", "fields": {
        "title": "共用任务", "assignee": [{"id": "ou_one"}, {"id": "ou_two"}, {"id": "ou_one"}],
        "deadline": 1790784000000, "load_score": 5}}]
    client.target = []
    client.get_records.side_effect = lambda table: deepcopy(client.source if table == "tasks" else client.target)

    def apply(table, action, records):
        """模拟服务端按记录持久化，以便验证多次对账收敛。"""
        assert table == "details"
        if action == "create":
            for item in records:
                client.target.append(deepcopy(item) | {"record_id": f"recDetail{len(client.target)}"})
        elif action == "update":
            for item in records:
                next(r for r in client.target if r["record_id"] == item["record_id"])["fields"].update(deepcopy(item["fields"]))
        else:
            client.target[:] = [r for r in client.target if r["record_id"] not in records]

    client.batch_records.side_effect = apply
    return client


def test_shared_full_score_and_idempotent_sync(client):
    assert sync_details(client) == {"created": 2, "updated": 0, "deleted": 0}
    assert [r["fields"]["score"] for r in client.target] == [5, 5]
    assert {r["fields"]["month"] for r in client.target} == {"2026-10"}
    for r in client.target:
        r["fields"]["person"][0]["name"] = "接口补充显示名"
        r["fields"]["score"] = "5.0"
    client.batch_records.reset_mock()
    assert sync_details(client) == {"created": 0, "updated": 0, "deleted": 0}
    client.batch_records.assert_not_called()


def test_manual_edits_completion_cancel_and_delete(client):
    sync_details(client)
    fields = client.source[0]["fields"]
    fields.update(title="调整任务", load_score=7, deadline=1793462400000,
                  assignee=[{"id": "ou_two"}, {"id": "ou_three"}], completed_at=1793462400000)
    assert sync_details(client) == {"created": 1, "updated": 1, "deleted": 1}
    assert all(r["fields"]["title"] == "调整任务" and r["fields"]["score"] == 7
               and r["fields"]["month"] == "2026-11" for r in client.target)
    fields["cancelled"] = True
    assert sync_details(client)["deleted"] == 2
    fields["cancelled"] = False
    sync_details(client)
    client.source = []
    assert sync_details(client)["deleted"] == 2


@pytest.mark.parametrize("score", [None, -1, "bad", True, float("inf"), 0])
def test_missing_invalid_and_zero_scores_keep_task_count(client, score):
    client.source[0]["fields"]["load_score"] = score
    sync_details(client)
    assert len(client.target) == 2
    assert all(r["fields"]["score"] == (0 if type(score) is int and score == 0 else None)
               for r in client.target)


@pytest.mark.parametrize("field,value", [("assignee", []), ("deadline", None)])
def test_unassigned_or_undated_no_monthly_detail(client, field, value):
    sync_details(client)
    client.source[0]["fields"][field] = value
    assert sync_details(client)["deleted"] == 2


def test_duplicate_details_removed_and_invalid_stored_score_corrected(client):
    sync_details(client)
    client.target.append(deepcopy(client.target[0]) | {"record_id": "recDuplicate"})
    client.target[0]["fields"]["score"] = -5
    assert sync_details(client) == {"created": 0, "updated": 1, "deleted": 1}


@pytest.mark.parametrize("failure", ["schema", "source_read", "target_read", "source_shape", "target_shape", "date"])
def test_incomplete_reads_or_untrusted_rows_never_write(client, failure):
    sync_details(client)
    client.batch_records.reset_mock()
    if failure == "schema":
        client.get_fields.side_effect = None
        client.get_fields.return_value = []
    elif failure == "source_read":
        client.get_records.side_effect = RuntimeError("读取失败")
    elif failure == "target_read":
        client.get_records.side_effect = [client.source, RuntimeError("目标读取失败")]
    elif failure == "source_shape":
        client.source[0]["fields"]["assignee"] = [{"name": "没有 ID"}]
    elif failure == "target_shape":
        client.target[0]["fields"]["key"] = "人工添加的明细"
    else:
        client.source[0]["fields"]["deadline"] = "非法时间"
    with pytest.raises((ValueError, RuntimeError)):
        sync_details(client)
    client.batch_records.assert_not_called()


def test_unknown_create_outcome_is_reconciled_next_time(client):
    apply = client.batch_records.side_effect

    def lost_response(table, action, records):
        """模拟创建成功但响应丢失。"""
        apply(table, action, records)
        raise RuntimeError("响应丢失")

    client.batch_records.side_effect = lost_response
    with pytest.raises(RuntimeError):
        sync_details(client)
    assert len(client.target) == 2
    client.batch_records.side_effect = apply
    assert sync_details(client) == {"created": 0, "updated": 0, "deleted": 0}


def test_background_failure_isolated_and_close_stops_thread(client, monkeypatch):
    started = threading.Event()
    attempts = []

    def fail(_):
        """首轮失败，下一轮仍能执行。"""
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("同步服务暂不可用")
        started.set()

    monkeypatch.setattr("app.task_details.sync_details", fail)
    monkeypatch.setattr("app.task_details.load_config", lambda _: ({}, 0.01))
    worker = TaskDetailsSync(client.settings)
    assert started.wait(3)
    worker.close()
    assert not worker._thread.is_alive()
    assert len(attempts) >= 2


def test_invalid_sync_config_and_source_target_cannot_write(client):
    path = client.settings.fields_path.parent / "task_details.json"
    config = json.loads(path.read_text())
    config["interval_seconds"] = 0
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_config(client.settings)
    with pytest.raises(ValueError):
        sync_details(client)
    client.batch_records.assert_not_called()


@pytest.mark.parametrize("action", ["create", "update", "delete"])
def test_batch_result_validation_and_no_empty_request(client, monkeypatch, action):
    feishu = FeishuClient(client.settings)
    monkeypatch.setattr(feishu, "_access_token", lambda: "token")
    record = "recOne" if action == "delete" else {"record_id": "recOne", "fields": {"score": 5}}
    request = Mock(return_value={"data": {"records": [{"record_id": "recOne", "deleted": True}]}})
    monkeypatch.setattr(feishu, "_request", request)
    feishu.batch_records("details", action, [record])
    assert request.call_args.args == ("POST", f"/bitable/v1/apps/base/tables/details/records/batch_{action}")
    request.return_value = {"data": {"records": []}}
    with pytest.raises(RuntimeError):
        feishu.batch_records("details", action, [record])
    request.reset_mock()
    feishu.batch_records("details", action, [])
    request.assert_not_called()


@pytest.mark.parametrize("data", [{"has_more": False}, {"items": []},
                                  {"items": [], "total": 3, "has_more": False}])
def test_malformed_page_is_not_an_empty_snapshot(client, monkeypatch, data):
    feishu = FeishuClient(client.settings)
    monkeypatch.setattr(feishu, "_access_token", lambda: "token")
    monkeypatch.setattr(feishu, "_request", Mock(return_value={"data": data}))
    with pytest.raises(RuntimeError):
        feishu.get_records("tasks")
    feishu._request.return_value = {"data": {"items": None, "total": 0, "has_more": False}}
    assert feishu.get_records("tasks") == []
