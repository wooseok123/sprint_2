import sys
from pathlib import Path

import pytest
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.parser import Point, Segment
from core.vision_cleanup import (
    GeminiVisionJudge,
    VisionCleanupCandidate,
    _build_outline_scan_payload,
    collect_vision_cleanup_candidates,
    maybe_apply_gemini_vision_cleanup,
)


class FakeJudge:
    def __init__(self, verdict_by_kind, suspicious_spans=None):
        self.model = "fake-gemini"
        self.verdict_by_kind = verdict_by_kind
        self.suspicious_spans = suspicious_spans or []

    def propose_suspicious_spans(self, *, outline_payload, max_spans):
        assert outline_payload["simplified_exterior_vertices"]
        return self.suspicious_spans[:max_spans]

    def judge_candidate(self, *, candidate, feature_payload):
        assert feature_payload["candidate_kind"] == candidate.kind
        assert feature_payload["wall_thickness_mm"] > 0
        verdict = self.verdict_by_kind.get(candidate.kind, self.verdict_by_kind.get("*", "keep"))
        return {
            "decision": verdict,
            "confidence": 0.95,
            "feature_type": "thin_spike" if verdict == "remove" else "legit_mass",
            "reason": f"fake:{candidate.kind}:{verdict}",
            "raw_text": '{"decision":"%s"}' % verdict,
        }


def _line(start, end, meta_type="line"):
    return Segment(Point(*start), Point(*end), {"type": meta_type})


def test_collect_vision_cleanup_candidates_finds_thin_feature():
    polygon = Polygon([
        (0, 0),
        (1000, 0),
        (1000, 250),
        (1040, 250),
        (1040, 310),
        (1000, 310),
        (1000, 600),
        (0, 600),
    ])
    segments = [
        _line((0, 0), (1000, 0)),
        _line((1000, 0), (1000, 250)),
        _line((1000, 250), (1040, 250)),
        _line((1040, 250), (1040, 310)),
        _line((1040, 310), (1000, 310)),
        _line((1000, 310), (1000, 600)),
        _line((1000, 600), (0, 600)),
        _line((0, 600), (0, 0)),
    ]

    candidates = collect_vision_cleanup_candidates(
        polygon=polygon,
        reference_segments=segments,
        wall_thickness=80.0,
    )

    assert candidates
    assert candidates[0].kind == "thin_feature"
    assert candidates[0].mask_polygon is not None
    assert candidates[0].bounds[2] >= 1035.0


def test_build_outline_scan_payload_uses_local_coordinates():
    polygon = Polygon([
        (1000, 2000),
        (1300, 2000),
        (1300, 2400),
        (1000, 2400),
    ])

    payload = _build_outline_scan_payload(polygon=polygon, wall_thickness=80.0)

    assert payload["world_origin"] == [1000.0, 2000.0]
    assert payload["local_bbox"] == [0.0, 0.0, 300.0, 400.0]
    assert payload["simplified_exterior_vertices"][0] == {"i": 0, "x": 0.0, "y": 0.0}


def test_gemini_requests_disable_thinking_and_force_json():
    judge = GeminiVisionJudge(api_key="test-key")
    recorded_payloads = []

    def fake_post(payload):
        recorded_payloads.append(payload)
        if len(recorded_payloads) == 1:
            return {"candidates": [{"content": {"parts": [{"text": '{"suspicious_spans":[]}' }]}}]}
        return {"candidates": [{"content": {"parts": [{"text": '{"decision":"keep","confidence":0.9,"feature_type":"legit_mass","reason":"ok"}'}]}}]}

    judge._post_generate_content = fake_post  # type: ignore[method-assign]

    spans = judge.propose_suspicious_spans(
        outline_payload={
            "local_bbox": [0.0, 0.0, 100.0, 100.0],
            "area_mm2": 10000.0,
            "perimeter_mm": 400.0,
            "wall_thickness_mm": 80.0,
            "simplified_vertex_count": 4,
            "simplified_exterior_vertices": [
                {"i": 0, "x": 0.0, "y": 0.0},
                {"i": 1, "x": 100.0, "y": 0.0},
                {"i": 2, "x": 100.0, "y": 100.0},
                {"i": 3, "x": 0.0, "y": 100.0},
            ],
        },
        max_spans=3,
    )
    verdict = judge.judge_candidate(
        candidate=VisionCleanupCandidate(
            kind="outline_suspect",
            source="test",
            bounds=(0.0, 0.0, 100.0, 100.0),
        ),
        feature_payload={"candidate_kind": "outline_suspect", "wall_thickness_mm": 80.0},
    )

    assert spans == []
    assert verdict["decision"] == "keep"
    assert len(recorded_payloads) == 2
    for payload in recorded_payloads:
        config = payload["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert config["thinkingConfig"] == {"thinkingBudget": 0}


def test_gemini_vision_cleanup_removes_thin_feature_when_judge_approves():
    polygon = Polygon([
        (0, 0),
        (1000, 0),
        (1000, 250),
        (1040, 250),
        (1040, 310),
        (1000, 310),
        (1000, 600),
        (0, 600),
    ])
    segments = [
        _line((0, 0), (1000, 0)),
        _line((1000, 0), (1000, 250)),
        _line((1000, 250), (1040, 250)),
        _line((1040, 250), (1040, 310)),
        _line((1040, 310), (1000, 310)),
        _line((1000, 310), (1000, 600)),
        _line((1000, 600), (0, 600)),
        _line((0, 600), (0, 0)),
    ]

    cleaned, metadata = maybe_apply_gemini_vision_cleanup(
        polygon=polygon,
        reference_segments=segments,
        wall_thickness=80.0,
        judge=FakeJudge({"thin_feature": "remove"}),
    )

    assert metadata["applied"] is True
    assert metadata["applied_count"] >= 1
    assert cleaned.bounds[2] == pytest.approx(1000.0, abs=2.0)
    assert cleaned.area < polygon.area


def test_gemini_vision_cleanup_keeps_legitimate_feature_when_judge_rejects():
    polygon = Polygon([
        (0, 0),
        (420, 0),
        (420, 170),
        (170, 170),
        (170, 420),
        (0, 420),
    ])
    segments = [
        _line((0, 0), (420, 0)),
        _line((420, 0), (420, 170)),
        _line((420, 170), (170, 170)),
        _line((170, 170), (170, 420)),
        _line((170, 420), (0, 420)),
        _line((0, 420), (0, 0)),
    ]

    cleaned, metadata = maybe_apply_gemini_vision_cleanup(
        polygon=polygon,
        reference_segments=segments,
        wall_thickness=40.0,
        judge=FakeJudge({"*": "keep"}),
    )

    assert metadata["attempted"] is False or metadata["applied"] is False
    assert cleaned.equals(polygon)


def test_gemini_vision_cleanup_can_add_global_outline_scan_candidates():
    polygon = Polygon([
        (0, 0),
        (800, 0),
        (800, 220),
        (930, 220),
        (930, 360),
        (800, 360),
        (800, 700),
        (0, 700),
    ])
    segments = [
        _line((0, 0), (800, 0)),
        _line((800, 0), (800, 220)),
        _line((800, 220), (930, 220)),
        _line((930, 220), (930, 360)),
        _line((930, 360), (800, 360)),
        _line((800, 360), (800, 700)),
        _line((800, 700), (0, 700)),
        _line((0, 700), (0, 0)),
    ]

    judge = FakeJudge(
        {"outline_suspect": "keep"},
        suspicious_spans=[
            {
                "start_idx": 2,
                "end_idx": 5,
                "confidence": 0.82,
                "feature_hint": "triangle_flap",
                "reason": "outward detour compared with neighboring long wall segments",
            }
        ],
    )

    cleaned, metadata = maybe_apply_gemini_vision_cleanup(
        polygon=polygon,
        reference_segments=segments,
        wall_thickness=40.0,
        judge=judge,
    )

    assert metadata["global_scan"]["attempted"] is True
    assert metadata["global_scan"]["proposed_count"] == 1
    assert metadata["global_scan"]["accepted_count"] == 1
    assert metadata["candidate_count"] >= 1
    assert metadata["attempts"][0]["source"] == "global_outline_scan"
    assert cleaned.equals(polygon)
