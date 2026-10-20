from __future__ import annotations

import math
import re
from collections import Counter
from typing import Callable

from .models import Skill

DEFAULT_SYNONYMS: dict[str, list[str]] = {
    "louder": ["volume", "sound"],
    "quieter": ["volume", "sound"],
    "mute": ["volume", "sound"],
    "weather": ["weather", "forecast", "search", "locations"],
    "forecast": ["weather", "forecast"],
    "rain": ["weather", "forecast"],
    "define": ["dictionary", "define"],
    "meaning": ["dictionary", "define"],
    "write": ["type", "note"],
    "type": ["type", "note"],
    "jot": ["type", "note"],
    "note": ["quick_note"],
    "brightness": ["brightness", "screen"],
    "dim": ["brightness", "screen"],
    "screenshot": ["screenshot"],
    "capture": ["screenshot"],
    "wifi": ["wifi", "password", "network"],
    "paper": ["arxiv", "paper"],
    "research": ["arxiv", "paper"],
    "doi": ["crossref", "work"],
    "citation": ["crossref", "work"],
    "install": ["app", "install"],
    "uninstall": ["app", "uninstall"],
    "open": ["app", "focus", "launch"],
    "look": ["wikipedia", "search", "summary"],
    "search": ["wikipedia", "search"],
    "find": ["wikipedia", "search"],
    "who": ["wikipedia", "summary"],
    "what": ["wikipedia", "summary"],
    "play": ["laya", "ultrafast", "browser", "youtube", "video"],
    "youtube": ["laya", "ultrafast", "browser", "youtube", "video"],
    "video": ["laya", "ultrafast", "browser", "youtube", "video"],
    "yesterday": ["recall", "memory", "history"],
    "did": ["recall", "memory"],
    "before": ["recall", "memory"],
}


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    out = []
    for w in words:
        out.append(_stem(w))
    return out


def _stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 3 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s"):
        return w[:-1]
    return w


def _doc_text(skill: Skill) -> str:
    return skill.name.replace("_", " ") + " " + (skill.description or "")


class SkillRouter:
    def __init__(
        self,
        skills: list[Skill],
        core: set[str],
        k: int = 15,
        embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
        synonyms: dict[str, list[str]] | None = None,
    ):
        self.skills = list(skills)
        self.core = set(core)
        self.k = k
        self.embed_fn = embed_fn
        self.synonyms = dict(DEFAULT_SYNONYMS)
        if synonyms:
            for key, val in synonyms.items():
                self.synonyms[key] = list(val)

        self._doc_texts = [_doc_text(s) for s in self.skills]
        self._doc_tokens = [_tokenize(t) for t in self._doc_texts]
        self._name_to_idx = {s.name: i for i, s in enumerate(self.skills)}

        n = len(self.skills)
        dfc = Counter()
        for toks in self._doc_tokens:
            for t in set(toks):
                dfc[t] += 1
        self._idf: dict[str, float] = {}
        for term, d in dfc.items():
            self._idf[term] = math.log((n - d + 0.5) / (d + 0.5) + 1.0)

        avgdl = sum(len(t) for t in self._doc_tokens) / max(n, 1)
        self._avgdl = avgdl
        self._k1 = 1.5
        self._b = 0.75

        self._doc_embeddings: list[list[float]] | None = None
        if self.embed_fn is not None:
            try:
                self._doc_embeddings = self.embed_fn(list(self._doc_texts))
            except Exception:
                self._doc_embeddings = None

    def _expand_query(self, request: str) -> str:
        tokens = re.findall(r"[A-Za-z0-9]+", request.lower())
        expanded = list(tokens)
        for t in tokens:
            if t in self.synonyms:
                expanded.extend(self.synonyms[t])
        return " ".join(expanded)

    def _keyword_scores(self, request: str) -> list[float]:
        expanded = self._expand_query(request)
        q_tokens = _tokenize(expanded)
        scores = []
        for idx, doc_toks in enumerate(self._doc_tokens):
            tfc = Counter(doc_toks)
            dl = len(doc_toks)
            score = 0.0
            for term in set(q_tokens):
                if term not in tfc:
                    continue
                idf_t = self._idf.get(term, 0.0)
                f = tfc[term]
                denom = f + self._k1 * (1 - self._b + self._b * dl / max(self._avgdl, 1e-9))
                score += idf_t * f * (self._k1 + 1) / denom
            scores.append(score)
        return scores

    def _cosine(self, a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def _normalize(self, values: list[float]) -> list[float]:
        if not values:
            return values
        mn = min(values)
        mx = max(values)
        if mx - mn == 0:
            return [0.0 for _ in values]
        return [(v - mn) / (mx - mn) for v in values]

    def _compute_scores(self, request: str) -> list[float]:
        kw = self._keyword_scores(request)
        if self.embed_fn is None or self._doc_embeddings is None:
            return kw
        try:
            q_emb = self.embed_fn([request])[0]
        except Exception:
            return kw
        emb = [self._cosine(q_emb, de) for de in self._doc_embeddings]
        kw_n = self._normalize(kw)
        emb_n = self._normalize(emb)
        return [0.5 * kw_n[i] + 0.5 * emb_n[i] for i in range(len(self.skills))]

    def route(self, request: str, history_skills: list[str] | None = None) -> list[Skill]:
        scores = self._compute_scores(request)
        indexed = list(enumerate(scores))
        order = sorted(indexed, key=lambda x: (-x[1], x[0]))

        selected_idx: set[int] = set()
        positive = [(i, s) for i, s in order if s > 0]
        for i, s in positive[: self.k]:
            selected_idx.add(i)

        if len(positive) < 5:
            for i, s in order[:10]:
                selected_idx.add(i)

        for name in self.core:
            if name in self._name_to_idx:
                selected_idx.add(self._name_to_idx[name])

        groups_to_expand = {"wikipedia", "open_meteo", "arxiv", "crossref"}
        selected_groups = set()
        for i in selected_idx:
            g = self.skills[i].group
            if g in groups_to_expand:
                selected_groups.add(g)
        for g in selected_groups:
            group_indices = [i for i, s in enumerate(self.skills) if s.group == g]
            group_sorted = sorted(group_indices, key=lambda i: (-scores[i], i))
            for i in group_sorted[:3]:
                selected_idx.add(i)

        if history_skills:
            for name in history_skills:
                if name in self._name_to_idx:
                    selected_idx.add(self._name_to_idx[name])

        selected_skills = [self.skills[i] for i in selected_idx]

        core_skills = [s for s in selected_skills if s.name in self.core]
        rest = [s for s in selected_skills if s.name not in self.core]
        core_skills.sort(key=lambda s: (-scores[self._name_to_idx[s.name]], self._name_to_idx[s.name]))
        rest.sort(key=lambda s: (-scores[self._name_to_idx[s.name]], self._name_to_idx[s.name]))
        seen = set()
        result = []
        for s in core_skills + rest:
            if s.name in seen:
                continue
            seen.add(s.name)
            result.append(s)
        return result

    def explain(self, request: str) -> list[tuple[str, float]]:
        scores = self._compute_scores(request)
        order = sorted(range(len(self.skills)), key=lambda i: (-scores[i], i))
        out = []
        for i in order[:25]:
            out.append((self.skills[i].name, float(scores[i])))
        return out
