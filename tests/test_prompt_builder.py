from winbrow_router.normalize import parse_skill_catalogue
from winbrow_router.prompt_builder import build_messages


def _skills():
    raw = open("catalogue.txt", encoding="utf-8").read()
    return parse_skill_catalogue(raw)


def test_volume_rule_absent_present():
    skills = _skills()
    routed_no_vol = [s for s in skills if s.name == "type_text"]
    msgs = build_messages("x", "Frontmost", ["File Explorer"], 22, [], routed_no_vol)
    sys = msgs["messages"][0]["content"]
    assert "Volume: use volume_set_level" not in sys

    routed_vol = [s for s in skills if s.name == "volume_set_level"]
    msgs2 = build_messages("x", "Frontmost", ["File Explorer"], 22, [], routed_vol)
    sys2 = msgs2["messages"][0]["content"]
    assert "Volume: use volume_set_level" in sys2


def test_frontmost_truncated():
    skills = _skills()
    routed = [s for s in skills if s.name == "type_text"]
    long_fm = "a" * 100
    msgs = build_messages("x", long_fm, ["File Explorer"], 22, [], routed)
    uc = msgs["messages"][1]["content"]
    assert "..." in uc


def test_background_apps_filter():
    skills = _skills()
    routed = [s for s in skills if s.name == "type_text"]
    apps = ["File Explorer", "AggregatorHost", "Foo Service"]
    msgs = build_messages("x", "Frontmost", apps, 22, [], routed)
    uc = msgs["messages"][1]["content"]
    assert "AggregatorHost" not in uc
    assert "Foo Service" not in uc
    assert "File Explorer" in uc
