"""Measure speaking patterns in a meeting transcript."""

from __future__ import annotations

import re
from collections import Counter, OrderedDict

from winbrow.skill_script_utils import clip, emit, fail, load_spec, read_text_arg, resolve_path

_SPEAKER = re.compile(
    r"^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*)?"
    r"(?P<speaker>[A-Z][A-Za-z0-9 .'_-]{0,40}?)\s*:\s*(?P<text>.+)$"
)
_FILLERS = (
    "um", "uh", "erm", "like", "you know", "i mean", "sort of", "kind of",
    "basically", "actually", "literally", "right",
)
_HEDGES = (
    "maybe", "perhaps", "i think", "i guess", "might", "could be", "probably",
    "not sure", "kind of", "sort of",
)
_CONFLICT_AVOID = (
    "i don't know", "whatever you think", "either way", "no worries",
    "it's fine", "let's not", "we can revisit", "not a big deal",
)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z']+", text.lower())


def analyze(transcript: str) -> dict:
    speakers: "OrderedDict[str, dict]" = OrderedDict()
    total_words = 0
    questions = 0
    for raw in transcript.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _SPEAKER.match(line)
        speaker = match.group("speaker").strip() if match else "Unknown"
        text = match.group("text").strip() if match else line
        words = _words(text)
        total_words += len(words)
        questions += text.count("?")
        entry = speakers.setdefault(speaker, {
            "words": 0, "turns": 0, "questions": 0, "fillers": 0, "hedges": 0,
        })
        entry["words"] += len(words)
        entry["turns"] += 1
        entry["questions"] += text.count("?")
        lowered = text.lower()
        entry["fillers"] += sum(lowered.count(word) for word in _FILLERS)
        entry["hedges"] += sum(lowered.count(word) for word in _HEDGES)

    for entry in speakers.values():
        entry["share"] = round(entry["words"] / total_words, 3) if total_words else 0
        entry["words_per_turn"] = round(entry["words"] / entry["turns"], 1) if entry["turns"] else 0

    lowered_all = transcript.lower()
    return {
        "total_words": total_words,
        "speakers": dict(speakers),
        "questions": questions,
        "filler_total": sum(entry["fillers"] for entry in speakers.values()),
        "hedge_total": sum(entry["hedges"] for entry in speakers.values()),
        "conflict_avoidance_signals": {
            phrase: lowered_all.count(phrase) for phrase in _CONFLICT_AVOID
            if lowered_all.count(phrase)
        },
        "turn_count": sum(entry["turns"] for entry in speakers.values()),
    }


def main() -> None:
    spec = load_spec()
    path_value = spec.get("path") or spec.get("input_path")
    if path_value:
        path = resolve_path(path_value)
        if not path.is_file():
            fail(f"Transcript not found: {path}")
        transcript = path.read_text(encoding="utf-8", errors="replace")
    else:
        transcript = read_text_arg(spec)
    if not transcript.strip():
        fail("Provide a transcript path or content.")

    data = analyze(transcript)
    # A compact human-readable summary makes the result useful without reading
    # the whole JSON blob in chat.
    lines = [
        f"Words: {data['total_words']} | Turns: {data['turn_count']} | Questions: {data['questions']}",
        "Speaking shares:",
    ]
    for speaker, entry in sorted(
        data["speakers"].items(), key=lambda item: item[1]["words"], reverse=True,
    ):
        lines.append(
            f"- {speaker}: {entry['share']:.0%} ({entry['words']} words, "
            f"{entry['turns']} turns, {entry['questions']} questions)"
        )
    if data["conflict_avoidance_signals"]:
        lines.append("Conflict-avoidance phrases: " + ", ".join(
            f"{key} x{value}" for key, value in data["conflict_avoidance_signals"].items()
        ))
    emit(True, clip("\n".join(lines)), data=data)


if __name__ == "__main__":
    main()
