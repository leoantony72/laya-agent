from __future__ import annotations

import re
from collections import Counter
from typing import List

from .models import Arg, Skill, RISKY_SKILLS


DUPLICATE_OF = {"open_notepad": "notepad_open", "computer_type": "type_text"}


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _clean_description(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", " ")
    idx = text.find("Returns")
    if idx != -1:
        text = text[:idx]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in sentences if "Although originally you did not have internet access" not in s]
    text = " ".join(kept)
    text = _collapse_ws(text)
    m = re.search(r"[.!?]", text)
    if m:
        first = text[: m.end()]
    else:
        first = text
    if len(first) > 120:
        first = first[:120]
    if not first.strip():
        first = text[:120]
    return _collapse_ws(first)


def _split_top_level(s: str) -> list[str]:
    parts = []
    depth = 0
    cur = ""
    for ch in s:
        if ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth = max(0, depth - 1)
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _parse_args(args_str: str) -> tuple[Arg, ...]:
    args_str = args_str.strip()
    if not args_str or args_str.lower() in ("no args", "noargs", ""):
        return ()
    args = []
    for part in _split_top_level(args_str):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([A-Za-z0-9_]+)\s*\(\s*(text|enum)\s*\)$", part, re.IGNORECASE)
        if m:
            name = m.group(1)
            typ = m.group(2).lower()
            args.append(Arg(name=name, type=typ, choices=()))
        else:
            m2 = re.match(r"^([A-Za-z0-9_]+)$", part)
            if m2:
                args.append(Arg(name=m2.group(1), type="text", choices=()))
    return tuple(args)


def parse_skill_catalogue(raw: str) -> list[Skill]:
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[str] = []
    current: str | None = None
    for line in raw.split("\n"):
        if line.startswith("- "):
            if current is not None:
                blocks.append(current)
            current = line[2:]
        else:
            if current is not None:
                current = current + "\n" + line
    if current is not None:
        blocks.append(current)

    parsed: list[dict] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        m = re.match(r"^([A-Za-z0-9_\-]+)\s*\(", block)
        if not m:
            continue
        name = m.group(1)
        i = block.index("(")
        depth = 0
        end = -1
        for j in range(i, len(block)):
            if block[j] == "(":
                depth += 1
            elif block[j] == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end == -1:
            continue
        args_str = block[i + 1 : end]
        rest = block[end + 1 :].strip()
        desc_raw = ""
        if rest.startswith(":"):
            desc_raw = rest[1:].strip()
        elif rest:
            desc_raw = rest
        desc = _clean_description(desc_raw)
        args = _parse_args(args_str)
        parsed.append({"name": name, "args": args, "description": desc})

    names_set = {p["name"] for p in parsed}
    to_drop = set()
    for p in parsed:
        n = p["name"]
        if not n.startswith("mcp_"):
            continue
        rest = n[4:]
        parts = rest.split("_")
        for i in range(1, len(parts)):
            server = "_".join(parts[:i])
            remainder = "_".join(parts[i:])
            if remainder.startswith(server + "_"):
                tool = remainder[len(server) + 1:]
                shorter = "mcp_" + server + "_" + tool
                if shorter in names_set and shorter != n:
                    to_drop.add(n)
                    break
    parsed = [p for p in parsed if p["name"] not in to_drop]

    by_name = {p["name"]: p for p in parsed}
    for alias, canonical in DUPLICATE_OF.items():
        if alias in by_name and canonical in by_name:
            parsed = [p for p in parsed if p["name"] != alias]
            by_name.pop(alias, None)

    first_seg_counts = Counter()
    for p in parsed:
        n = p["name"]
        if not n.startswith("mcp_"):
            seg = n.split("_", 1)[0] if "_" in n else n
            first_seg_counts[seg] += 1

    skills: list[Skill] = []
    for p in parsed:
        n = p["name"]
        if n.startswith("mcp_"):
            rest = n[4:]
            if rest.startswith("open_meteo") or rest.startswith("openmeteo"):
                group = "open_meteo"
            else:
                server = rest.split("_", 1)[0] if "_" in rest else rest
                group = server
        else:
            seg = n.split("_", 1)[0] if "_" in n else n
            group = seg if first_seg_counts.get(seg, 0) >= 2 else "misc"
        skills.append(
            Skill(
                name=n,
                args=p["args"],
                description=p["description"],
                group=group,
                risky=n in RISKY_SKILLS,
            )
        )
    return skills


def render_skill_line(skill: Skill) -> str:
    if not skill.args:
        args_str = "no args"
    else:
        parts = []
        for a in skill.args:
            if a.type == "enum":
                if a.choices:
                    parts.append(f"{a.name}{{{ '|'.join(a.choices)}}}")
                else:
                    parts.append(f"{a.name}{{enum}}")
            else:
                parts.append(a.name)
        args_str = ", ".join(parts)
    line = f"- {skill.name}({args_str}): {skill.description}"
    if skill.risky:
        line += " [risky]"
    return line
