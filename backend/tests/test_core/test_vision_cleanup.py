import sys
from pathlib import Path

import pytest
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.parser import Point, Segment
from core.vision_cleanup import collect_vision_cleanup_candidates, cv2, maybe_apply_gemini_vision_cleanup


class FakeJudge:
    def __init__(self, verdict_by_kind):
        self.model = "fake-gemini"
        self.verdict_by_kind = verdict_by_kind

    def judge_candidate(self, *, image_bytes, candidate, metrics):
        assert image_bytes
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


@pytest.mark.skipif(cv2 is None, reason="cv2 is not installed")
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


@pytest.mark.skipif(cv2 is None, reason="cv2 is not installed")
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


@pytest.mark.skipif(cv2 is None, reason="cv2 is not installed")
def test_gemini_vision_cleanup_can_use_preferred_door_bounds():
    polygon = Polygon([
        (0, 0),
        (1000, 0),
        (1000, 200),
        (1040, 230),
        (1070, 270),
        (1080, 320),
        (1070, 370),
        (1040, 410),
        (1000, 440),
        (1000, 600),
        (0, 600),
    ])
    segments = [
        _line((0, 0), (1000, 0)),
        _line((1000, 440), (1000, 600)),
        _line((1000, 600), (0, 600)),
        _line((0, 600), (0, 0)),
    ]

    cleaned, metadata = maybe_apply_gemini_vision_cleanup(
        polygon=polygon,
        reference_segments=segments,
        wall_thickness=40.0,
        preferred_bounds=(995.0, 210.0, 1085.0, 415.0),
        judge=FakeJudge({"door_like": "remove"}),
    )

    assert metadata["attempted"] is True
    assert metadata["applied"] is True
    assert cleaned.area < polygon.area
    assert metadata["attempts"][0]["source"] == "bridge_preferred"
