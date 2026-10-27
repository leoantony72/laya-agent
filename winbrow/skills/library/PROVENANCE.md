# Provenance

Converted on 2026-10-25. Only the six requested README sections were used.
"Rewritten" means the behavior was reimplemented for WinBrow; upstream text and
scripts were not copied verbatim. Check each upstream repository for its own
license before redistribution.

| Skill | Source | License | Conversion |
|---|---|---|---|
| docx | anthropics/skills/tree/main/skills/docx | Proprietary | Rewritten; upstream workflow + extended python-docx helper |
| pdf | anthropics/skills/tree/main/skills/pdf | Proprietary | Rewritten; pure-Python pypdf/fpdf2/pdfplumber helper; upstream forms/reference docs vendored under `references/` |
| pptx | anthropics/skills/tree/main/skills/pptx | Proprietary | Rewritten; python-pptx helper + design guidance |
| algorithmic-art | anthropics/skills/tree/main/skills/algorithmic-art | See upstream LICENSE.txt | Rewritten; p5.js viewer + scaffold helper; upstream templates referenced |
| skill-creator | anthropics/skills/tree/main/skills/skill-creator | See upstream LICENSE.txt | Rewritten for WinBrow frontmatter; new scaffold/validate helpers |
| html-ppt | lewislulu/html-ppt-skill | MIT | Vendored assets/templates/references; new Windows scaffold + PDF/PNG exporter |
| xlsx | anthropics/skills/tree/main/skills/xlsx | Proprietary | Rewritten; openpyxl/pandas helper |
| markdown-to-epub | smerchek/claude-epub-skill | See upstream | Rewritten; stdlib EPUB 3 builder |
| legal-workflows | sboghossian/master-claude-for-legal | See upstream | Rewritten; 6 workflow references, 3 templates |
| csv-summarizer | coffeefuelbump/csv-data-summarizer-claude-skill | See upstream | Rewritten; pandas/matplotlib helper |
| deep-research | sanjay3290/ai-skills/tree/main/skills/deep-research | Apache-2.0 | Rewritten; Gemini Search-grounding helper |
| postgres | sanjay3290/ai-skills/tree/main/skills/postgres | Apache-2.0 | Rewritten; read-only psycopg2 helper |
| recursive-research | Anjos2/recursive-research | See upstream | Rewritten; translated from Spanish |
| root-cause-tracing | obra/superpowers/tree/main/skills/systematic-debugging | See upstream | Source link 404; adapted the renamed systematic-debugging skill |
| article-extractor | michalparkola/tapestry-skills-for-claude-code/article-extractor | See upstream | Rewritten to browser/fetch workflow |
| brainstorming | obra/superpowers/tree/main/skills/brainstorming | See upstream | Rewritten; removed subagent/planning handoff |
| content-research-writer | ComposioHQ/awesome-claude-skills | Apache-2.0 | Rewritten |
| family-history | emaynard/claude-family-history-research-skill | See upstream | Rewritten |
| meeting-insights | ComposioHQ/awesome-claude-skills | Apache-2.0 | Rewritten; stdlib metrics helper |
| notebooklm | PleasePrompto/notebooklm-skill | See upstream | Rewritten to browser automation; experimental |
| twitter-optimizer | ComposioHQ/awesome-claude-skills | AGPL-3.0 reference | Rewritten; no upstream code |
| anydesign | uxKero/anydesign | See upstream | Rewritten |
| canvas-design | ComposioHQ/awesome-claude-skills | See upstream LICENSE.txt | Rewritten; Pillow renderer; no font pack copied |
| imagen | sanjay3290/ai-skills/tree/main/skills/imagen | Apache-2.0 | Rewritten; Gemini REST helper |
| image-enhancer | ComposioHQ/awesome-claude-skills | Apache-2.0 | Rewritten; Pillow helper |
| slack-gif | ComposioHQ/awesome-claude-skills | See upstream LICENSE.txt | Rewritten; Pillow GIF builder |
| theme-factory | ComposioHQ/awesome-claude-skills | See upstream LICENSE.txt | Rewritten; 10 themes condensed into one reference |
| video-downloader | ComposioHQ/awesome-claude-skills | Apache-2.0 | Rewritten; yt-dlp helper |
| youtube-transcript | michalparkola/tapestry-skills-for-claude-code/youtube-transcript | See upstream | Rewritten; yt-dlp captions helper |
| swiftui-design | wholiver/swiftui-design-skill | See upstream | Rewritten; translated from Chinese; code-only on Windows |
| pixelbin-media | anandpareek-hub/pixelbin-claude-skill | See upstream | Rewritten; thin authenticated client |
| file-organizer | ComposioHQ/awesome-claude-skills | Apache-2.0 | Rewritten; stdlib duplicate/plan helper |
| invoice-organizer | ComposioHQ/awesome-claude-skills | Apache-2.0 | Rewritten; pure-Python PDF extraction helper |
| kaizen | NeoLabHQ/context-engineering-kit/plugins/kaizen/skills/kaizen | See upstream | Rewritten |
| n8n-workflows | haunchen/n8n-skills | See upstream | Reconstructed from repo docs/release pack; no 545-node dataset copied |
| raffle-winner | ComposioHQ/awesome-claude-skills | Apache-2.0 | Rewritten; stdlib secrets helper |
| solo-founder | rockscy/solo-skills | See upstream | Rewritten; 7 references |
| tailored-resume | ComposioHQ/awesome-claude-skills | Apache-2.0 | Rewritten |
| ship-learn-next | michalparkola/tapestry-skills-for-claude-code/ship-learn-next | See upstream | Rewritten |
| tapestry | michalparkola/tapestry-skills-for-claude-code | See upstream | Source link 404; reconstructed from README description |
| git-pushing | mhattingpete/claude-skills-marketplace/engineering-workflow-plugin/skills/git-pushing | See upstream | Rewritten |
| google-workspace | sanjay3290/ai-skills/tree/main/skills | Apache-2.0 | Rewritten; 7 references; unified OAuth helper |
| mercury | teamoffsite.ai/proton/docs/skill | Unknown | Source URL redirects to an empty skills index; reconstructed from the entry description |
| outline | sanjay3290/ai-skills/tree/main/skills/outline | Apache-2.0 | Rewritten; REST helper |
| review-implementing | mhattingpete/claude-skills-marketplace/engineering-workflow-plugin/skills/review-implementing | See upstream | Rewritten |
| test-fixing | mhattingpete/claude-skills-marketplace/engineering-workflow-plugin/skills/test-fixing | See upstream | Rewritten |
