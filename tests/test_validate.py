import json
from winbrow_router.normalize import parse_skill_catalogue
from winbrow_router.validate import validate_plan


def _skills():
    raw = open("catalogue.txt", encoding="utf-8").read()
    return parse_skill_catalogue(raw)


def test_fenced_accepted():
    skills = _skills()
    routed = [s for s in skills if s.name == "type_text"]
    text = "```json\n{\"kind\":\"chat\",\"response\":\"hi\"}\n```"
    plan, errs = validate_plan(text, routed, skills)
    assert plan is not None
    assert not [e for e in errs if not e.startswith("warning:")]


def test_each_error_type():
    skills = _skills()
    routed = [s for s in skills if s.name == "type_text"]
    # invalid json
    p, e = validate_plan("{bad", routed, skills)
    assert p is None and any("invalid JSON" in x for x in e)
    # invalid kind
    p, e = validate_plan('{"kind":"nope"}', routed, skills)
    assert p is None and any("invalid kind" in x for x in e)
    # no steps
    p, e = validate_plan('{"kind":"action","steps":[]}', routed, skills)
    assert p is None and any("step" in x.lower() for x in e)
    # too many steps
    p, e = validate_plan('{"kind":"action","steps":[{"skill":"type_text","args":{"text":"a"}},{"skill":"type_text","args":{"text":"b"}},{"skill":"type_text","args":{"text":"c"}},{"skill":"type_text","args":{"text":"d"}}]}', routed, skills)
    assert p is None
    # chat no response
    p, e = validate_plan('{"kind":"chat"}', routed, skills)
    assert p is None and any("response" in x for x in e)
    # hallucinated
    p, e = validate_plan('{"kind":"action","steps":[{"skill":"ghost_skill","args":{}}]}', routed, skills)
    assert p is None and any("hallucinated" in x for x in e)
    # router miss
    p, e = validate_plan('{"kind":"action","steps":[{"skill":"volume_set_level","args":{"level":"5"}}]}', routed, skills)
    assert p is None and any("router_miss" in x for x in e)
    # missing arg
    p, e = validate_plan('{"kind":"action","steps":[{"skill":"type_text","args":{}}]}', routed, skills)
    assert p is None and any("missing required arg" in x for x in e)
    # unknown arg
    p, e = validate_plan('{"kind":"action","steps":[{"skill":"type_text","args":{"foo":"bar","text":"x"}}]}', routed, skills)
    assert p is None and any("unknown arg" in x for x in e)


def test_risky_auto_fix():
    skills = _skills()
    risky = [s for s in skills if s.risky][0]
    routed_all = skills
    args = {a.name: "x" for a in risky.args} if risky.args else {}
    text = json.dumps({"kind": "action", "steps": [{"skill": risky.name, "args": args}]})
    plan, errs = validate_plan(text, routed_all, skills)
    assert plan is not None
    assert plan["steps"][0].get("confirm") is True
