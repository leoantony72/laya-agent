# Image search / download sources — research (2026-10-29)

> **Status:** Phase 1 is implemented: `ddgs` general web search is the default
> for real subjects, Wallhaven is art/anime/wallpaper-only, NekosAPI is never
> an automatic fallback, and Wikimedia 429s are reported instead of silently
> becoming "no results". Phase 2 (Pixabay/Unsplash keys) is optional and not
> implemented yet.

## Why the current tool looks "anime-only"

`winbrow/image_tools.py` has three sources and a fixed fallback chain:

```
Wallhaven (art/wallpaper site, categories=111 => general + anime + people)
  -> Wikimedia Commons (licensed photos)
  -> NekosAPI (random safe anime)
```

Consequences:

- Wallhaven is the **primary** source. It is an art/wallpaper site, so many
  subjects come back as anime/illustration even when the "general" category is
  enabled. `categories=111` explicitly includes anime.
- If Wallhaven times out or returns nothing, Wikimedia is tried. Commons has
  excellent licensed photos but weak coverage for everyday/abstract subjects
  ("office interior", "invoice", "headphones", "product shot"), and it
  rate-limits rapid API use (HTTP 429; observed locally).
- The last fallback is NekosAPI, which is **anime by definition**. So any query
  that exhausts the first two sources becomes anime. That is the observed
  "everything is anime" failure mode. NekosAPI should never be an automatic
  fallback for a non-anime request.

Local evidence (2026-10-29, this machine):

- `download_images("office")` failed entirely: Wallhaven timed out, Wikimedia
  returned no usable results (Commons later returned HTTP 429 after repeated
  calls), NekosAPI was unreachable.
- `download_images("cat"/"dog"/"car"/"golden retriever")` did return real
  photos from Wallhaven, not anime. The failure is query-dependent and happens
  when the art source has a bad match or fails.
- Raw Wikimedia search does have "golden retriever" photos, but the current
  helper silently turns 429/empty responses into "no matches" because it does
  not inspect the status before parsing.

## What "general image search" needs

Two different jobs are being conflated:

1. **Personal image downloads** — "get me pictures of X". Coverage matters more
   than licensing; web image search is appropriate, but the user should see the
   source page and a "check rights" warning.
2. **Document/deck assets** — images inserted into PDFs/PPTX. These must be
   reusable (CC/PD/stock license) and carry attribution. `winbrow/image_search.py`
   already handles this for Wikimedia Commons.

The fix should not make document workflows download random copyrighted images.

## Existing tools and services

| Option | Key? | Coverage | License metadata | Limits / cost | Verdict for WinBrow |
|---|---|---|---|---|---|
| **DuckDuckGo Images via `ddgs`** | No | Broad general web (Bing-backed metasearch) | Optional `license_image` filter; otherwise unknown | Unofficial; rate limits/CAPTCHA; no SLA | **Best keyless general source.** MIT, actively maintained, returns image URL + source page + dimensions. Tested locally: golden retriever, office interior, invoice, headphones all returned real results in ~1–2 s. |
| **Bing Images HTML (`bing_image_downloader`)** | No | Broad general web | Unknown | Unofficial; markup can change; no SLA | Works locally (3 real golden retriever photos, 1080×1080/1280×720). Already installed here, not in requirements. Good fallback if `ddgs` is blocked. |
| **`icrawler` (Bing/Google/Baidu/Flickr)** | No (Flickr key for Flickr) | Broad general web; filters for size/color/type/license | Unknown | Older project; crawler threads; brittle | Works locally (3 Eiffel Tower photos, 2624×3936 etc.). MIT. Good backup; more machinery than needed. |
| **Openverse API** | No (anonymous 20/min, 200/day) | CC/PD aggregate: Flickr, Wikimedia, museums, StockSnap, etc.; `category=photograph` | **Yes**, full attribution + license | 20/min, 200/day anonymous; 100/min/10k/day with app | Best license-safe general-ish source for documents. **However `api.openverse.org` timed out from this machine** (both IPv4 and curl_cffi); it may be network-blocked. Use with a short timeout and fall through. |
| **Wikimedia Commons** | No | Encyclopedic real photos; uneven for everyday subjects | **Yes** (CC/PD) | Generous but 429s on bursts; needs descriptive UA/caching | Keep for licensed/document use. Add 429 handling and caching. |
| **Wallhaven** | No | Anime/art/wallpapers, some photography | No | 45 req/min | Keep **only** for wallpaper/anime/art intent. Exclude `category=anime` for real subjects (`categories=101` or `100`), and filter post-hoc. |
| **NekosAPI** | No | Anime only | No | — | Only when the user asks for anime, or `source=nekosapi`. Never auto. |
| **Pixabay API** | Free key (instant) | High-quality stock photos/illustrations | Pixabay license; attribution appreciated | 100 req/60 s; **24 h response cache required** | Strong optional source for real photos. Add only when `PIXABAY_API_KEY` is set. |
| **Unsplash API** | Free key | High-quality photos | Unsplash license; attribution + download tracking rules | 50 req/h demo; 1k–5k/h production | Optional; demo key is enough for a local agent but approval/attribution rules matter. `source.unsplash.com` is dead (sunset 2024). |
| **Pexels API** | Free key | High-quality photos | Pexels license; attribution | 200/h, 20k/month | **New key issuance is paused** (as of Oct 2026); skip for new installs. |
| **Brave Search API** | Key | General web image index | Unknown; check source | $5/1k requests; $5 free credit/month | Production-grade paid fallback, official API. |
| **Serper.dev** | Key | Google Images JSON | Unknown | 2,500 free queries; then paid | High-quality paid fallback. |
| **Google Custom Search JSON API** | Key | General web images | Unknown | 100/day | **Closed to new customers** (Feb 2026); existing users until Jan 2027. Skip. |
| **Bing Search API (official)** | Key | General web | — | — | **Retired 11 Aug 2025.** Dead. |
| **gallery-dl** | No | Galleries/sites (Pixiv, Twitter, Reddit, etc.), not keyword search | — | — | Use for direct gallery/site URLs, not "find images of X". |
| **LoremFlickr** | No | Random Flickr by tag | No | Failed locally (HTTP 500) | Not reliable. |
| **Lorem Picsum** | No | Random placeholder photos | Unsplash | — | Placeholders only, no subject search. |

## Recommendation

### Source policy (intent-aware)

```
art/anime/wallpaper intent?
  -> Wallhaven (categories=111 or 010, SFW)
  -> NekosAPI only if the query is actually anime
  -> web search (ddgs)

real-world subject / photo / anything else?
  -> web search (ddgs, photo filter)
  -> Wikimedia Commons (licensed photos)
  -> Wallhaven (categories=101: general + people, NO anime)
  -> explicit error (never anime)
```

- `download_images` default should be the **web search** path, not Wallhaven.
- `download_images(source="wallhaven")` remains available for art/wallpapers.
- `source="nekosapi"` remains explicit only.
- For document insertion (`image_search.py`), keep/expand the licensed
  Openverse/Wikimedia path and require credits.

### Phase 1 — keyless fix (recommended first)

1. Add `ddgs>=9.16,<10` to `requirements.txt` (MIT; pure Python wheels).
2. Add `search_web_images()` using `DDGS().images(...)` with:
   - `safesearch="moderate"`, `type_image="photo"`, `size="Large"`,
     `max_results=count * 2`;
   - optional `license_image` filter for document-safe results;
   - map results to `ImageCandidate` with `source`, `page`, `width`,
     `height`, and `license="unknown"` (plus a source-page URL);
   - pass the source page as `Referer` when downloading (some CDNs 403
     without it).
3. Make `source="auto"` intent-aware (policy above). Remove NekosAPI from the
   non-anime fallback. Add `categories`/category filtering to Wallhaven.
4. Handle HTTP 429 from Wikimedia with `Retry-After` and a short cache; stop
   turning rate limits into silent "no results".
5. Add a small in-memory TTL cache (10–30 min) per query to avoid re-hitting
   search engines and to make repeated agent retries cheap.
6. Return explicit source-attempt errors and never silently fall back to anime.
7. Update tool description/examples and tests.

### Phase 2 — optional quality/licensing (only if keys are provided)

- `PIXABAY_API_KEY`: stock photos, 100/min, cache 24 h, attribution.
- `UNSPLASH_ACCESS_KEY`: demo 50/h, attribution + download tracking.
- Openverse: add as a licensed source with an 8 s timeout; do not let it block
  the web path (it timed out on this machine).
- Add a `license`/`purpose` option so document workflows can require
  reusable images.

### Phase 3 — production reliability (optional paid)

- **Brave Search API** (`X-Subscription-Token`, `/res/v1/images/search`,
  `safesearch=strict`) or **Serper.dev** (Google Images).
- Use only when `BRAVE_SEARCH_API_KEY` / `SERPER_API_KEY` is configured; they
  provide official/structured results and an SLA that scraped Bing/DDG cannot.

## Phase 1 changes in this repo

| File | Change |
|---|---|
| `requirements.txt` | add `ddgs>=9.16,<10` |
| `winbrow/image_tools.py` | add `search_web_images`, intent classification, Wallhaven category control, remove NekosAPI from auto, pass `Referer`, cache |
| `winbrow/tests/test_image_tools.py` | mock `ddgs`; assert web-first for real subjects; anime-only fallback; Wallhaven category filters; 429 handling |
| `winbrow/brain/prompt.py` | update image rule: `download_images` now does general web search by default; art/anime is explicit |
| `winbrow/image_search.py` | optional Openverse source for licensed/document assets; 429 handling |
| `.env.example` | document optional `PIXABAY_API_KEY`, `UNSPLASH_ACCESS_KEY`, `BRAVE_SEARCH_API_KEY` |
| `IMAGE_SOURCES.md` | this research note |

## Key risk notes

- `ddgs` and `bing_image_downloader` are **scrapers**, not official APIs.
  They can be rate-limited, CAPTCHA'd, or broken by markup changes. Always
  validate bytes with Pillow and provide a clear error; do not retry forever.
- Search-engine images are usually **copyrighted**. Show the source page and
  `license: unknown`; do not imply reuse rights. For documents, use
  Openverse/Wikimedia/Pixabay/Unsplash with attribution.
- Wallhaven is an art site, not a general photo search. Keeping it first for
  "cat" or "office" is the root cause of the anime bias.
