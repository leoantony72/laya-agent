from __future__ import annotations

import math
import re


def count_tokens(text: str) -> int:
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        pass
    total = 0
    for m in re.finditer(r"[A-Za-z]+", text):
        total += math.ceil(len(m.group(0)) / 5.5)
    for m in re.finditer(r"\d+", text):
        total += math.ceil(len(m.group(0)) / 3)
    for ch in text:
        if not ch.isalnum() and not ch.isspace():
            total += 1
    return total
