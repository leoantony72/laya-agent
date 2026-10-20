from __future__ import annotations

import argparse
import json
from pathlib import Path

from .normalize import parse_skill_catalogue
from .router import SkillRouter
from .prompt_builder import build_messages
from .tokens import count_tokens


def main(argv=None):
    parser = argparse.ArgumentParser(prog="winbrow_router.eval")
    parser.add_argument("catalogue", help="path to catalogue text file")
    parser.add_argument("cases", help="path to eval_cases.jsonl")
    parser.add_argument("--k", type=int, default=15)
    args = parser.parse_args(argv)

    raw = Path(args.catalogue).read_text(encoding="utf-8")
    skills = parse_skill_catalogue(raw)
    core = set()
    router = SkillRouter(skills, core, k=args.k)

    cases = []
    with open(args.cases, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))

    total = len(cases)
    hits = 0
    routed_token_sum = 0
    full_token_sum = 0
    failures = []

    for case in cases:
        request = case["request"]
        expected = case["expected"]
        routed = router.route(request)
        routed_names = {s.name for s in routed}
        ok = all(e in routed_names for e in expected)
        if ok:
            hits += 1
        else:
            failures.append((request, expected, router.explain(request)))

        dummy_frontmost = "Desktop"
        dummy_apps = ["File Explorer"]
        dummy_volume = 50
        dummy_history = []
        routed_msgs = build_messages(request, dummy_frontmost, dummy_apps, dummy_volume, dummy_history, routed)
        full_msgs = build_messages(request, dummy_frontmost, dummy_apps, dummy_volume, dummy_history, skills)
        routed_text = routed_msgs["messages"][0]["content"] + "\n" + routed_msgs["messages"][1]["content"]
        full_text = full_msgs["messages"][0]["content"] + "\n" + full_msgs["messages"][1]["content"]
        routed_token_sum += count_tokens(routed_text)
        full_token_sum += count_tokens(full_text)

    recall = hits / total if total else 0.0
    avg_routed = routed_token_sum / total if total else 0.0
    avg_full = full_token_sum / total if total else 0.0

    print(f"cases: {total}")
    print(f"recall@{args.k}: {recall:.3f} ({hits}/{total})")
    print(f"avg routed prompt tokens: {avg_routed:.1f}")
    print(f"full-catalogue prompt tokens: {avg_full:.1f}")
    print(f"failing cases: {len(failures)}")
    for req, exp, expl in failures:
        print(f"\nFAIL: {req}")
        print(f"  expected: {exp}")
        print(f"  explain: {expl[:10]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
