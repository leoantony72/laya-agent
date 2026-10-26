"""Run a cited research pass through the Gemini API with Google Search grounding."""

from __future__ import annotations

import os

from winbrow.skill_script_utils import clip, emit, fail, integration_secret, load_spec, require_module

httpx = require_module("httpx", "httpx")


def research(spec: dict) -> None:
    key = str(
        spec.get("api_key")
        or integration_secret("gemini", "api_key")
        or os.environ.get("GEMINI_API_KEY")
        or ""
    ).strip()
    if not key:
        fail(
            "Gemini is not configured. Add an API key in the Integrations panel "
            "or set GEMINI_API_KEY."
        )
    query = str(spec.get("query") or "").strip()
    if not query:
        fail("A research query is required.")

    model = str(
        spec.get("model")
        or integration_secret("gemini", "model")
        or "gemini-2.5-flash"
    ).strip()
    output_format = str(spec.get("format") or "").strip()
    prompt = (
        "You are a careful research assistant. Research the question using web "
        "search. Return a concise, factual report. Separate claims that are well "
        "supported from claims that are uncertain. Include a short source list "
        "with titles and URLs. Never invent a source.\n\n"
        f"QUESTION: {query}"
    )
    if output_format:
        prompt += f"\n\nREQUIRED STRUCTURE:\n{output_format}"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": float(spec.get("temperature") or 0.2),
            "maxOutputTokens": int(spec.get("max_output_tokens") or 4096),
        },
    }
    try:
        response = httpx.post(
            url,
            params={"key": key},
            json=payload,
            timeout=float(spec.get("timeout") or 120),
        )
    except Exception as exc:
        fail(f"Gemini request failed: {exc}")
    if response.status_code >= 400:
        detail = response.text[:500]
        fail(f"Gemini returned HTTP {response.status_code}: {detail}")

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        fail("Gemini returned no candidates. The prompt may have been blocked.")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "\n".join(str(part.get("text") or "") for part in parts).strip()
    if not text:
        fail("Gemini returned an empty answer.")

    sources = []
    metadata = candidates[0].get("groundingMetadata") or {}
    for chunk in metadata.get("groundingChunks") or []:
        web = chunk.get("web") or {}
        if web.get("uri"):
            sources.append({
                "title": str(web.get("title") or ""),
                "url": str(web.get("uri") or ""),
            })
    if sources:
        lines = ["", "SOURCES:"]
        seen = set()
        for source in sources:
            if source["url"] in seen:
                continue
            seen.add(source["url"])
            lines.append(f"- {source['title'] or source['url']}: {source['url']}")
        text += "\n".join(lines)

    emit(True, clip(text), data={"sources": sources, "model": model})


def main() -> None:
    research(load_spec())


if __name__ == "__main__":
    main()
