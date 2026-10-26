# Session Patterns

## Web Access Patterns (added 2026-10-26)

Research / information tasks → ALWAYS web_search, NEVER browser
Product comparison → ALWAYS web_search, NEVER browser
Price lookup → web_search first, web_fetch if URL known
Documentation reading → web_fetch if URL known, web_search first
Playing media → browser_navigate required
Explicit "go to" / "open" requests → browser_navigate required
File upload → browser_navigate required
Login + action → browser_navigate required

THE MOST COMMON BUG: brain opens browser for research tasks.
Every time this happens it costs 10+ extra steps and 30+ seconds.
web_search completes the same task in 1 step and 200ms.
