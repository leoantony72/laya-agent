"""WinBrow bundled keyless MCP servers (stdlib-only, no MCP SDK).

One runtime many APIs: `python tools/mcp_servers/winbrow_keyless.py --api dictionary`.
Speaks line-delimited JSON-RPC 2.0 on stdio: initialize / initialized / ping /
tools/list / tools/call. Every tool hits a FREE public HTTP API — no keys ever.

APIs (--api): dictionary openlibrary nominatim rest-countries frankfurter
usgs hacknews github mdn gdelt reddit pdf
"""
import json
import re
import sys
import time
import urllib.parse as up
import urllib.request as ur

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = "WinBrowAgent/6.0 (desktop assistant; contact: local)"
GENERIC_TIMEOUT = 15
MAX_TEXT = 4096
MAX_PDF_TEXT = 24_000


def _get(url: str, headers: dict = None) -> str:
    req = ur.Request(url, headers={"User-Agent": UA, "Accept": "application/json",
                                   **(headers or {})})
    with ur.urlopen(req, timeout=GENERIC_TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def _jget(url: str, headers: dict = None):
    return json.loads(_get(url, headers))


def _clip(o, n: int = MAX_TEXT) -> str:
    s = o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)
    return s[:n]


# ------------------------------------------------------------------ pdf (local, read-only)
def _pdf_reader(path):
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise ValueError
        except Exception as exc:
            raise ValueError("PDF is password-protected; cannot read it") from exc
    return reader


def _pdf_info(args):
    reader = _pdf_reader(args.get("path", ""))
    meta = reader.metadata or {}

    def field(key):
        try:
            return meta.get(key)
        except Exception:
            return None

    toc = []

    def walk(items, level=1):
        for item in items or []:
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:
                page = 0
            toc.append({"level": level,
                        "title": getattr(item, "title", str(item)),
                        "page": page})

    try:
        walk(reader.outline)
    except Exception:
        toc = []

    return {"pages": len(reader.pages), "encrypted": bool(reader.is_encrypted),
            "metadata": {key: field(key) for key in
                         ("/Title", "/Author", "/Subject", "/Producer",
                          "/CreationDate")},
            "toc": toc[:20]}


def _pdf_extract_text(args):
    reader = _pdf_reader(args.get("path", ""))
    total = len(reader.pages)
    first = max(1, int(args.get("first_page", 1) or 1))
    last = int(args.get("last_page", 0) or 0) or total
    last = min(last, total)
    parts = []
    for i in range(first - 1, last):
        parts.append(f"--- page {i + 1} ---\n{reader.pages[i].extract_text() or ''}")
    return "".join(parts)


def _pdf_search(args):
    reader = _pdf_reader(args.get("path", ""))
    q = str(args.get("query", "")).lower()
    cap = min(int(args.get("max_hits", 20) or 20), 20)
    hits = []
    if not q:
        return hits
    for i, page in enumerate(reader.pages):
        txt = page.extract_text() or ""
        low = txt.lower()
        at = low.find(q)
        if at >= 0:
            hits.append({"page": i + 1,
                         "snippet": txt[max(0, at - 60):at + 120]
                         .replace("\n", " ")})
            if len(hits) >= cap:
                break
    return hits


# ------------------------------------------------------------------ handlers
def _dictionary(args):
    """Datamuse (dictionaryapi.dev times out on some networks). md=dp → defs+phonetics."""
    w = str(args.get("word", ""))
    q = up.quote(w)
    d = _jget(f"https://api.datamuse.com/words?sp={q}&md=dp&max=3")
    out = {"word": w}
    if d:
        first = d[0]
        out["phonetic"] = next((t[3:] for t in (first.get("tags") or [])
                                if t.startswith("ipa:")), "")
        out["meaning"] = [s.replace("\t", ": ") for s in (first.get("defs") or [])[:3]]
    else:
        out["meaning"] = []
    return out


def _dictionary_syn(args):
    q = up.quote(str(args.get("word", "")))
    d = _jget(f"https://api.datamuse.com/words?rel_syn={q}&max=10")
    return {"synonyms": [x.get("word") for x in d][:10]}


def _openlibrary_search(args):
    q = up.quote(str(args.get("query", "")))
    d = _jget(f"https://openlibrary.org/search.json?q={q}&limit=5")
    return {"numFound": d.get("numFound"),
            "books": [{"title": b.get("title"),
                       "author": (b.get("author_name") or ["?"])[0],
                       "year": (b.get("first_publish_year") or "?"),
                       "key": b.get("key")} for b in (d.get("docs") or [])[:5]]}


def _openlibrary_isbn(args):
    i = str(args.get("isbn", "")).replace("-", "").strip()
    d = _jget(f"https://openlibrary.org/isbn/{i}.json")
    return {"title": d.get("title"), "publish_date": d.get("publish_date"),
            "publishers": d.get("publishers"), "pages": d.get("number_of_pages")}


def _nominatim(args):
    q = up.quote(str(args.get("query", "")))
    d = _jget(f"https://nominatim.openstreetmap.org/search?q={q}"
              f"&format=json&limit=3")
    return [{"display_name": r.get("display_name"),
             "type": r.get("type"),
             "lat": r.get("lat"), "lon": r.get("lon")} for r in d]


_ATLAS_CACHE: list = []


def _atlas():
    """countries.trevorblades.com schema changed (no name filter) — one-shot
    full list, cached per process."""
    global _ATLAS_CACHE
    if _ATLAS_CACHE:
        return _ATLAS_CACHE
    q = "query { countries { code name capital currencies languages { name } } }"
    req = ur.Request("https://countries.trevorblades.com/",
                     data=json.dumps({"query": q}).encode(),
                     headers={"User-Agent": UA, "Content-Type": "application/json"})
    with ur.urlopen(req, timeout=25) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    _ATLAS_CACHE = (d.get("data") or {}).get("countries") or []
    return _ATLAS_CACHE


def _rest_countries(args):
    """Country facts via country atlas + World Bank population (restcountries.com v3.1 retired)."""
    want = str(args.get("name", "")).strip().lower()
    c = next((x for x in _atlas() if (x.get("name") or "").lower() == want), None)
    if c is None:
        c = next((x for x in _atlas()
                  if want and (want in (x.get("name") or "").lower())), None)
    if c is None:
        return {"name": want, "data": None, "message": "country not found"}
    pop = ""
    try:
        code = c.get("code", "")
        p = _jget(f"https://api.worldbank.org/v2/country/{code}"
                  f"/indicator/SP.POP.TOTL?format=json&per_page=1")
        pr = (p[1] or [{}])[0] if isinstance(p, list) and p[1] else {}
        pop = pr.get("value")
    except Exception:
        pass
    return {"name": c.get("name"), "capital": c.get("capital"),
            "currencies": c.get("currencies"),
            "languages": [l.get("name") for l in c.get("languages") or []],
            "population": pop}


def _frankfurter(args):
    base = str(args.get("from", "USD")).upper()
    to = str(args.get("to", "EUR")).upper()
    amt = str(args.get("amount", "1"))
    d = _jget(f"https://api.frankfurter.dev/v1/latest?amount={amt}"
              f"&base={base}&symbols={to}")
    return {"amount": amt, "base": base,
            "rates": d.get("rates"), "date": d.get("date")}


def _usgs(args):
    mag = args.get("min_magnitude", 5)
    lim = min(int(args.get("limit", 5) or 5), 10)
    d = _jget(f"https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
              f"&minmagnitude={mag}&limit={lim}&orderby=time")
    return [{"place": f.get("properties", {}).get("place"),
             "mag": f.get("properties", {}).get("mag"),
             "time_utc": f.get("properties", {}).get("time"),
             "url": f.get("properties", {}).get("url")}
            for f in (d.get("features") or [])[:lim]]


def _hn_search(args):
    q = up.quote(str(args.get("query", "")))
    d = _jget(f"https://hn.algolia.com/api/v1/search?query={q}&tags=story")
    return {"hits_n": d.get("nbHits"),
            "stories": [{"title": h.get("title"), "url": h.get("url"),
                         "points": h.get("points"), "comments": h.get("num_comments"),
                         "id": h.get("objectID")} for h in (d.get("hits") or [])[:5]]}


def _hn_front(args):
    d = _jget("https://hn.algolia.com/api/v1/search_by_date?tags=front_page")
    return [{"title": h.get("title"), "points": h.get("points"),
             "url": h.get("url")} for h in (d.get("hits") or [])[:8]]


def _github_search(args):
    q = up.quote(str(args.get("query", "")))
    d = _jget(f"https://api.github.com/search/repositories?q={q}&per_page=5",
              headers={"Accept": "application/vnd.github+json"})
    return {"total": d.get("total_count"),
            "repos": [{"full_name": r.get("full_name"), "stars": r.get("stargazers_count"),
                       "language": r.get("language"), "url": r.get("html_url"),
                       "desc": (r.get("description") or "")[:120]}
                      for r in (d.get("items") or [])[:5]]}


def _mdn(args):
    q = up.quote(str(args.get("query", "")))
    d = _jget(f"https://developer.mozilla.org/api/v1/search?q={q}&locale=en-US")
    docs = []
    for n in (d.get("documents") or [])[:5]:
        docs.append({"title": n.get("title"), "slug": n.get("mdn_url"),
                     "summary": (n.get("summary") or "")[:200]})
    return {"docs": docs}


def _gdelt(args):
    """GDELT 2.0 DOC API, shared per-IP throttle — 2 tries within a 30s budget."""
    q = up.quote(str(args.get("query", "")))
    last = None
    for i in range(2):
        try:
            d = _jget(f"https://api.gdeltproject.org/api/v2/doc/doc?query={q}"
                      f"&mode=artlist&maxrecords=5&format=json&sort=datedesc")
            return [{"title": a.get("title"), "domain": a.get("domain"),
                     "url": a.get("url"), "date": a.get("seendate")}
                    for a in (d.get("articles") or [])[:5]]
        except Exception as e:
            last = e
            time.sleep(3.0)
    raise last


def _reddit(args):
    """Reddit public JSON is 403-blocked for non-browser agents — RSS feed stays open.
    Minimal ATOM parse (stdlib only)."""
    q = up.quote(str(args.get("query", "")))
    xml = _get(f"https://www.reddit.com/search.rss?q={q}",
               headers={"Accept": "application/atom+xml"})
    entries = re.findall(r"<entry>(.*?)</entry>", xml, re.S)[:5]

    def val(entry, tag):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", entry, re.S)
        return m.group(1).strip() if m else ""
    out = []
    for e in entries:
        link_m = re.search(r'href="(https://[^"]+)"', e)
        sub_m = re.search(r"/r/([A-Za-z0-9_]+)", e)
        title = val(e, "title").replace("&amp;", "&")
        out.append({"title": title[:120],
                    "subreddit": sub_m.group(1) if sub_m else "",
                    "url": link_m.group(1) if link_m else ""})
    return out


# ------------------------------------------------------------------ catalog
def _tool(name, desc, props, req, fn):
    return {"name": name, "description": desc, "fn": fn,
            "inputSchema": {"type": "object", "properties": props,
                            "required": req}}


APIS = {
    "dictionary": [
        _tool("define",
              "Dictionary meaning and phonetics for an English word (Datamuse).",
              {"word": {"type": "string", "description": "English word to define"}},
              ["word"], _dictionary),
        _tool("synonyms",
              "Synonyms for a word (Datamuse).",
              {"word": {"type": "string", "description": "English word"}},
              ["word"], _dictionary_syn),
    ],
    "openlibrary": [
        _tool("search_books", "Search Open Library: titles, authors, first year.",
              {"query": {"type": "string", "description": "Book/author search phrase"}},
              ["query"], _openlibrary_search),
        _tool("book_isbn", "Fetch book metadata by ISBN.",
              {"isbn": {"type": "string", "description": "ISBN-10 or ISBN-13"}},
              ["isbn"], _openlibrary_isbn),
    ],
    "nominatim": [
        _tool("geocode", "Resolve a place/address to coordinates via OpenStreetMap.",
              {"query": {"type": "string", "description": "Place or address"}},
              ["query"], _nominatim),
    ],
    "rest-countries": [
        _tool("country_info",
              "Country facts: capital, currencies, languages, population.",
              {"name": {"type": "string", "description": "Country name (e.g. France)"}},
              ["name"], _rest_countries),
    ],
    "frankfurter": [
        _tool("fx_convert", "Currency conversion with latest ECB reference rates.",
              {"amount": {"type": "string", "description": "Amount to convert"},
               "from": {"type": "string", "description": "Base currency (e.g. USD)"},
               "to": {"type": "string", "description": "Target currency (e.g. EUR)"}},
              ["from", "to"], _frankfurter),
    ],
    "usgs": [
        _tool("earthquakes", "Recent significant earthquakes from the USGS catalog.",
              {"min_magnitude": {"type": "number", "description": "Minimum magnitude"},
               "limit": {"type": "integer", "description": "Max results (<=10)"}},
              [], _usgs),
    ],
    "hacknews": [
        _tool("front_page", "Current Hacker News front page.",
              {}, [], _hn_front),
        _tool("search", "Search Hacker News stories (Algolia index).",
              {"query": {"type": "string", "description": "Search phrase"}},
              ["query"], _hn_search),
    ],
    "github": [
        _tool("search_repos",
              "Search PUBLIC GitHub repositories (unauthenticated, rate-limited 60/h).",
              {"query": {"type": "string", "description": "GitHub repo search query"}},
              ["query"], _github_search),
    ],
    "mdn": [
        _tool("docs_search", "Search MDN Web Docs (HTML/CSS/JS/Web APIs references).",
              {"query": {"type": "string", "description": "Docs search phrase"}},
              ["query"], _mdn),
    ],
    "gdelt": [
        _tool("news_search", "Global news article search via GDELT 2.0 DOC API.",
              {"query": {"type": "string", "description": "News search phrase"}},
              ["query"], _gdelt),
    ],
    "reddit": [
        _tool("search_posts", "Search Reddit posts via public JSON endpoints.",
              {"query": {"type": "string", "description": "Search phrase"}},
              ["query"], _reddit),
    ],
    "pdf": [
        _tool("pdf_extract_text",
              "Extract text from a LOCAL PDF file (page-range optional).",
              {"path": {"type": "string", "description": "Absolute path to the .pdf file"},
               "first_page": {"type": "integer", "description": "First page (1-based, optional)"},
               "last_page": {"type": "integer", "description": "Last page inclusive (optional)"}},
              ["path"], _pdf_extract_text),
        _tool("pdf_info",
              "LOCAL PDF metadata: page count, encryption, title/author, table of contents.",
              {"path": {"type": "string", "description": "Absolute path to the .pdf file"}},
              ["path"], _pdf_info),
        _tool("pdf_search",
              "Search a LOCAL PDF for a phrase; returns page numbers + snippets.",
              {"path": {"type": "string", "description": "Absolute path to the .pdf file"},
               "query": {"type": "string", "description": "Text to find in the PDF"},
               "max_hits": {"type": "integer", "description": "Max hits (default 20, cap 20)"}},
              ["path", "query"], _pdf_search),
    ],
}


# ------------------------------------------------------------------ server
def serve(api: str) -> None:
    tools = APIS.get(api)
    if not tools:
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid = req.get("method", ""), req.get("id")

        def reply(result, rid=rid):
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                         "result": result}) + "\n")
            sys.stdout.flush()

        if method == "initialize":
            reply({"protocolVersion": "2025-06-18",
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": f"winbrow-{api}", "version": "1.0.0"}})
        elif method == "ping":
            reply({})
        elif method == "tools/list":
            reply({"tools": [{k: t[k] for k in ("name", "description", "inputSchema")}
                             for t in tools]})
        elif method == "tools/call":
            params = req.get("params") or {}
            tname, targs = params.get("name"), params.get("arguments") or {}
            tspec = next((t for t in tools if t["name"] == tname), None)
            if tspec is None:
                reply({"isError": True,
                       "content": [{"type": "text",
                                    "text": f"unknown tool '{tname}'"}]})
            else:
                try:
                    reply({"content": [{"type": "text",
                                        "text": _clip(tspec["fn"](targs))}]})
                except Exception as e:
                    reply({"isError": True,
                           "content": [{"type": "text",
                                        "text": f"{e.__class__.__name__}: {e}"}]})


if __name__ == "__main__":
    _api = "dictionary"
    _argv = sys.argv[1:]
    if "--api" in _argv:
        _i = _argv.index("--api")
        if _i + 1 < len(_argv):
            _api = _argv[_i + 1].replace("_", "-") if False else _argv[_i + 1]
    _api = {"usgs-earthquake": "usgs", "usgs_earthquake": "usgs",
            "rest-countries": "rest-countries", "rest_countries": "rest-countries",
            "hacker-news": "hacknews", "hacker_news": "hacknews",
            "github-public": "github", "github_public": "github",
            "mdn-compat": "mdn", "mdn_compat": "mdn",
            "open-library": "openlibrary", "open_library": "openlibrary"
            }.get(_api, _api)
    serve(_api)