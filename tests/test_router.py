from winbrow_router.normalize import parse_skill_catalogue
from winbrow_router.router import SkillRouter


def _skills():
    raw = open("catalogue.txt", encoding="utf-8").read()
    return parse_skill_catalogue(raw)


def test_open_notepad_type_hello():
    skills = _skills()
    core = set()
    router = SkillRouter(skills, core, k=15)
    res = router.route("open notepad and type hello")
    names = [s.name for s in res]
    assert "app_focus_or_launch" in names
    assert "type_text" in names
    assert len(res) <= 15 + len(core) + 6


def test_make_it_louder_history():
    skills = _skills()
    router = SkillRouter(skills, set(), k=15)
    res = router.route("make it louder", history_skills=["volume_set_level"])
    names = [s.name for s in res]
    assert "volume_set_level" in names


def test_group_expansion_wikipedia():
    skills = _skills()
    router = SkillRouter(skills, set(), k=15)
    res = router.route("who is Ada Lovelace")
    names = [s.name for s in res]
    wiki = [n for n in names if "wikipedia" in n]
    assert len(wiki) >= 3
