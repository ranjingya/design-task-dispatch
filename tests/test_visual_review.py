"""验证总览编号、多轮补图、关键图片覆盖和上下文保留。"""

import copy
import json
from unittest.mock import Mock

import httpx
import pytest
from PIL import Image

from app.ai import AIClient
from app.config import ROOT, Settings
from app.visual_review import VisualReview, detail_images, image_message, visual_config
from test_rating import RATED, RULES, OPTIONS


@pytest.fixture
def settings(tmp_path):
    return Settings("app", "secret", "base", "table", "token", tmp_path / "fields.json", rules_table_id="rules", ai_key="key", ai_model="test")


@pytest.fixture
def config():
    return visual_config(json.loads((ROOT / "config/materials.json").read_text()))


def images_and_sources(count, groups=1, required=()):
    images = [image_message(Image.new("RGB", (100, 100), (index * 7 % 256, 100, 50))) for index in range(count)]
    sources = [{"image_index": i + 1, "source": f"图片 {i + 1}", "group": f"工作表 {i % groups}",
                "context": "商品参考", "requires_detail": i + 1 in required} for i in range(count)]
    return images, sources


def response(result):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]})


def test_overview_complete_and_first_round_spans_groups(config):
    images, sources = images_and_sources(31, 4)
    plan = VisualReview(images, sources, config)
    assert sorted(i for page in plan.overviews for i in page["image_indices"]) == list(range(1, 32))
    keys, batch = plan.take(first=True)
    assert len(batch) == 5
    assert len({plan.overviews[int(key.split(":")[1]) - 1]["group"] for key in keys}) == 4
    assert len(plan.pending()) == 3


def test_instruction_priority_and_long_image_parts(config):
    images, sources = images_and_sources(8, 8, required=(8,))
    fragments = detail_images(Image.new("RGB", (400, 4500), "white"), config)
    assert len(fragments) == 3
    parts = [[image] for image in images]
    parts[-1] = fragments
    plan = VisualReview(images, sources, config, parts)
    keys, _ = plan.take(first=True)
    assert plan.overviews[int(keys[0].split(":")[1]) - 1]["image_indices"] == [8]
    assert all(f"image:8:{n}" in plan.pending() for n in range(1, 4))


def test_on_demand_original_keeps_context_and_stops_early(settings, monkeypatch):
    images, sources = images_and_sources(8)
    requests = {"action": "request_images", "requests": [{"kind": "image", "id": 8, "reason": "看清说明"}]}
    snapshots = []
    responses = iter([response(requests), response(RATED)])
    def post(*args, **kwargs):
        snapshots.append(copy.deepcopy(kwargs["json"]))
        return next(responses)
    monkeypatch.setattr("app.ai.httpx.post", post)
    result = AIClient(settings).classify({"image_sources": sources}, RULES, OPTIONS, images, "测试部")
    assert result["matched"]
    assert len(snapshots) == 2
    first = snapshots[0]["messages"][1]
    assert len(first["content"]) == 3
    second = snapshots[1]["messages"]
    assert second[1] == first
    assert json.loads(second[2]["content"]) == requests
    assert json.loads(second[3]["content"][0]["text"])["visual_review"]["current_images"] == ["image:8:1"]


def test_final_cannot_bypass_required_original(settings, monkeypatch):
    images, sources = images_and_sources(2, required=(2,))
    post = Mock(side_effect=[response(RATED), response(RATED)])
    monkeypatch.setattr("app.ai.httpx.post", post)
    assert AIClient(settings).classify({"image_sources": sources}, RULES, OPTIONS, images, "测试部")["matched"]
    assert post.call_count == 2
    state = json.loads(post.call_args.kwargs["json"]["messages"][-1]["content"][0]["text"])["visual_review"]
    assert state["current_images"] == ["image:2:1"]


def test_round_limit_five_ten_ten_and_no_false_match(settings, monkeypatch):
    images, sources = images_and_sources(31, required=range(1, 32))
    snapshots = []
    def post(*args, **kwargs):
        snapshots.append(copy.deepcopy(kwargs["json"]))
        return response(RATED)
    monkeypatch.setattr("app.ai.httpx.post", post)
    result = AIClient(settings).classify({"image_sources": sources}, RULES, OPTIONS, images, "测试部")
    assert not result["matched"] and result["work_type"] is None
    assert "轮次上限" in result["unmatched_reason"]
    states = [json.loads(p["messages"][-1]["content"][0]["text"])["visual_review"] for p in snapshots]
    assert [len(state["current_images"]) for state in states] == [5, 10, 10]
    assert len({key for state in states for key in state["current_images"]}) == 25


def test_unenlarged_business_images_allow_match(settings, monkeypatch):
    images, sources = images_and_sources(20)
    post = Mock(return_value=response(RATED))
    monkeypatch.setattr("app.ai.httpx.post", post)
    assert AIClient(settings).classify({"image_sources": sources}, RULES, OPTIONS, images, "测试部")["matched"]
    assert post.call_count == 1


def test_invalid_or_repeated_requests(config):
    images, sources = images_and_sources(1)
    plan = VisualReview(images, sources, config)
    for item in ({"kind": "image", "id": 2, "reason": "检查"}, {"kind": "image", "id": True, "reason": "检查"},
                 {"kind": "other", "id": 1, "reason": "检查"}):
        with pytest.raises(ValueError):
            plan.request([item])
    plan.take(first=True)
    request = [{"kind": "image", "id": 1, "reason": "检查"}]
    plan.request(request)
    plan.take()
    plan.request(request)
    assert not plan.pending()


def test_plain_scan_or_image_reference_requires_detail(config):
    images, sources = images_and_sources(6)
    plan = VisualReview(images, sources, config, demand={"description": "具体要求见图"})
    plan.take(first=True)
    assert len(plan.pending()) == 6


def test_selection_phase_defers_rules_until_coverage(settings, monkeypatch):
    images, sources = images_and_sources(31, 4)
    calls = []
    replies = iter([response({"action": "request_images", "requests": [{"kind": "overview", "id": 8, "reason": "检查未读总览"}]}), response(RATED)])
    def post(*args, **kwargs):
        calls.append(copy.deepcopy(kwargs["json"]))
        return next(replies)
    monkeypatch.setattr("app.ai.httpx.post", post)
    assert AIClient(settings).classify({"image_sources": sources}, RULES, OPTIONS, images, "测试部")["matched"]
    first = json.loads(calls[0]["messages"][-1]["content"][0]["text"])
    last = json.loads(calls[1]["messages"][-1]["content"][0]["text"])
    assert "rules" not in first and last["rules"] == RULES
    assert len(first["visual_review"]["current_images"]) == 5
    assert len(last["visual_review"]["current_images"]) == 3
    assert not last["visual_review"]["pending_required"]
