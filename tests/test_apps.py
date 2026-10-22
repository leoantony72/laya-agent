from winbrow.apps import AppEntry, _best_match, clean_query
from winbrow.router import _clean_app_phrase


def _entries():
    return [
        AppEntry(name="Cursor", target="cursor.exe", kind="exe"),
        AppEntry(name="OBS Studio", target="obs64.exe", kind="exe"),
        AppEntry(name="Obsidian", target="obsidian.exe", kind="exe"),
        AppEntry(name="Visual Studio Code", target="code.exe", kind="exe",
                 aliases=("code", "vscode", "vs code")),
        AppEntry(name="Google Chrome", target="chrome.exe", kind="exe",
                 aliases=("chrome",)),
        AppEntry(name="Photos", target="photos:app", kind="appid"),
        AppEntry(name="Notepad", target="notepad.exe", kind="exe"),
        AppEntry(name="Notepad++", target="notepad++.exe", kind="exe"),
        AppEntry(name="Paint", target="mspaint.exe", kind="exe"),
        AppEntry(name="Paint.NET", target="paintdotnet.exe", kind="exe"),
        AppEntry(name="Brave Browser", target="brave.exe", kind="exe",
                 aliases=("brave",)),
        AppEntry(name="Microsoft Store", target="ms-windows-store:",
                 kind="protocol", aliases=("store",)),
    ]


def _match(utterance):
    phrase = _clean_app_phrase(utterance)
    return _best_match(clean_query(phrase), _entries())


def test_exact_app_name_wins():
    match = _match("could you open cursor")
    assert match is not None
    assert match.entry.name == "Cursor"
    assert match.score == 1.0


def test_trailing_please_is_ignored():
    match = _match("open obs studio please")
    assert match is not None
    assert match.entry.name == "OBS Studio"
    assert match.score == 1.0


def test_short_name_prefers_exact_token_over_longer_prefix():
    match = _match("open obs")
    assert match is not None
    assert match.entry.name == "OBS Studio"


def test_acronym_and_typo_match():
    assert _match("open vsc").entry.name == "Visual Studio Code"
    assert _match("open curser").entry.name == "Cursor"
    assert _match("open chorme").entry.name == "Google Chrome"


def test_unrelated_word_does_not_match():
    assert _match("photoshop") is None
    assert _match("winbrow_no_such_app_xyz") is None


def test_symbol_products_are_not_collapsed():
    assert _match("open notepad").entry.name == "Notepad"
    assert _match("open notepad++").entry.name == "Notepad++"
    assert _match("open paint").entry.name == "Paint"
    assert _match("open paint.net").entry.name == "Paint.NET"


def test_generic_category_only_matches_exact_alias():
    assert _match("open browser") is None
    assert _match("open web browser") is None
    assert _match("open store").entry.name == "Microsoft Store"
