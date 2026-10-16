"""父子任务的人员归属、完成释放、整组排除及统计同步验证。"""

from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from app.config import ROOT, Settings
from app.recommendation import workloads
from app.statistics import statistics_rows, sync_statistics
from app.task_records import parent_ids, task_snapshot

M = json.loads((ROOT / 'config/fields.json').read_text())
C = json.loads((ROOT / 'config/statistics.json').read_text())


def root():
    return {'record_id': 'recParent', 'fields': {'任务名称': '多人需求', '最终负责人': [{'id': 'a'}, {'id': 'b'}],
            '组长确认': True, '负责人确认': True, '任务负载分': 6, '截止时间': 1000,
            '父记录': [{'record_ids': None, 'text': None}]}}


def child(rid='recChild', owner='a', **values):
    return {'record_id': rid, 'fields': {'父记录': [{'record_ids': ['recParent']}],
            '最终负责人': [{'id': owner}], **values}}


@pytest.mark.parametrize('value,expected', [
    (None, []), ([{'record_ids': None, 'text': None}], []),
    ([{'id': 'recParent'}], ['recParent']), ([{'record_ids': ['recParent']}], ['recParent']),
])
def test_link_wrapper(value, expected):
    assert parent_ids({'父记录': value}, M) == expected


def test_child_completion_only_releases_its_owner_and_ignores_child_score():
    records = [root(), child(实际完成时间=200, 任务负载分=999)]
    summary, rows = task_snapshot(records, M)
    assert len(rows) == 2 and summary[0]['fields']['实际完成时间'] is None
    loads = workloads(records, M, ['a', 'b'], 'other')
    assert loads['a'] == {'score': 0, 'count': 0, 'missing': []}
    assert loads['b'] == {'score': 6, 'count': 1, 'missing': []}
    assert '实际完成时间' not in records[0]['fields']
    assert rows[0]['fields']['任务负载分'] == 6


def test_all_finished_summary_uses_last_person_and_parent_remains_unchanged():
    records = [root(), child(实际完成时间=200), child('recSecond', 'b', 实际完成时间=300)]
    original = deepcopy(records)
    summary, _ = task_snapshot(records, M)
    assert summary[0]['fields']['实际完成时间'] == 300
    assert records == original
    assert all(value['score'] == 0 for value in workloads(records, M, ['a', 'b'], 'other').values())


def test_blank_children_and_unrelated_owner_keep_parent_load():
    blank = child()
    blank['fields'].pop('最终负责人')
    records = [root(), blank, child('recOther', 'outsider', 实际完成时间=100)]
    assert [value['score'] for value in workloads(records, M, ['a', 'b'], 'other').values()] == [6, 6]


@pytest.mark.parametrize('excluded', ['recParent', 'recChild'])
def test_excludes_whole_family(excluded):
    assert all(value['count'] == 0 for value in workloads([root(), child()], M, ['a', 'b'], excluded).values())


def test_duplicate_children_do_not_double_count_or_release_early():
    records = [root(), child(实际完成时间=100), child('recDuplicate')]
    assert workloads(records, M, ['a'], 'other')['a']['score'] == 6


def test_cancellation_missing_score_and_recommendation_reservation():
    parent = root()
    parent['fields']['任务负载分'] = None
    records = [parent, child(是否取消=True)]
    assert workloads(records, M, ['a', 'b'], 'other')['a']['count'] == 0
    assert workloads(records, M, ['a', 'b'], 'other')['b']['missing'] == ['recParent']
    parent['fields'].update({'组长确认': False, '系统推荐人': [{'id': 'b'}], '任务负载分': 6})
    loads = workloads([parent], M, ['a', 'b'], 'other')
    assert loads['a']['score'] == 0 and loads['b']['score'] == 6


def test_statistics_has_one_summary_and_one_row_per_person():
    rows = statistics_rows([root(), child(实际完成时间=100)], M, C)
    assert len(rows) == 3
    assert rows['summary:recParent']['实际完成时间'] is None
    assert rows['person:recParent:a']['实际完成时间'] == 100
    assert rows['person:recParent:a']['任务负载分'] == 6


def test_statistics_reconciles_existing_rows_without_touching_demands(tmp_path):
    (tmp_path / 'fields.json').write_text(json.dumps(M))
    (tmp_path / 'statistics.json').write_text(json.dumps(C))
    settings = Settings('app', 'secret', 'base', 'tasks', 'token', tmp_path / 'fields.json',
                        rules_cache_path=tmp_path / 'rules.json', statistics_table_id='stats')
    client = Mock()
    client.settings = settings
    desired = statistics_rows([root()], M, C)
    old = [{'record_id': 'recSummary', 'fields': desired['summary:recParent']},
           {'record_id': 'recOld', 'fields': {'统计键': 'person:deleted:a'}},
           {'record_id': 'recManual', 'fields': {'统计键': '人工记录'}}]
    client.get_records.side_effect = lambda table: [root()] if table == 'tasks' else old
    assert sync_statistics(client) == {'created': 2, 'updated': 0, 'deleted': 1}
    assert client.mutate_statistics.call_args_list[-1].args == ('batch_delete', ['recOld'])
    client.update_record.assert_not_called()
    client.settings = replace(settings, statistics_table_id='tasks')
    with pytest.raises(ValueError):
        sync_statistics(client)
