"""创建人部门归属与通讯录缓存测试。"""

from unittest.mock import Mock

import pytest

from app.departments import resolve_department
from app.feishu import FeishuClient


@pytest.mark.parametrize('creator', [None, [], {}, [{'id': 'a'}, {'id': 'b'}]])
def test_invalid_creator_skips_contact(creator):
    client = Mock()
    assert resolve_department(creator, [], client) is None
    client.get_contact_user.assert_not_called()


@pytest.mark.parametrize('roots,nodes,expected', [
    (['team'], {'team': ('工作组', 'middle'), 'middle': ('中间层', 'business'), 'business': ('业务部', '0')}, '业务部'),
    (['business'], {'business': ('业务部', '0')}, '业务部'),
    (['one', 'two'], {'one': ('一组', 'business'), 'two': ('二组', 'business'), 'business': ('业务部', '0')}, '业务部'),
    (['one', 'two'], {'one': ('业务部', '0'), 'two': ('另一部', '0')}, None),
    (['other'], {'other': ('未知部', '0')}, None),
    (['one', 'other'], {'one': ('业务部', '0'), 'other': ('未知部', '0')}, None),
    (['one'], {'one': ('一组', 'two'), 'two': ('二组', 'one')}, None),
])
def test_walks_all_memberships(roots, nodes, expected):
    client = Mock()
    client.get_contact_user.return_value = {'department_ids': roots}
    client.get_contact_department.side_effect = lambda key: dict(zip(('name', 'parent_department_id'), nodes[key]))
    rules = [{'departments': ['业务部', '另一部']}]
    assert resolve_department({'id': 'creator'}, rules, client) == expected


@pytest.mark.parametrize('failure', ['user', 'department', 'missing_parent', 'missing_membership'])
def test_incomplete_contact_is_unknown(failure):
    client = Mock()
    client.get_contact_user.return_value = {'department_ids': ['team']}
    client.get_contact_department.return_value = {'name': '某组'}
    if failure == 'user':
        client.get_contact_user.side_effect = RuntimeError('无权限')
    elif failure == 'department':
        client.get_contact_department.side_effect = RuntimeError('无权限')
    elif failure == 'missing_membership':
        client.get_contact_user.return_value = {}
    assert resolve_department({'id': 'creator'}, [{'departments': ['业务部']}], client) is None


def test_contact_cache_expires_and_retries_missing_fields(monkeypatch):
    client = FeishuClient(Mock())
    client._access_token = Mock(return_value='token')
    response = {'data': {'user': {'department_ids': ['dept']}}}
    client._request = Mock(return_value=response)
    now = [100]
    monkeypatch.setattr('app.feishu.time.monotonic', lambda: now[0])
    client.get_contact_user('a')
    client.get_contact_user('a')
    assert client._request.call_count == 1
    now[0] += 86401
    client.get_contact_user('a')
    assert client._request.call_count == 2
    client._request.return_value = {'data': {'user': {}}}
    client.get_contact_user('b')
    client.get_contact_user('b')
    assert client._request.call_count == 4
