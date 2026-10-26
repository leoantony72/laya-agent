---
name: anydesign
skill: playbook_anydesign
kind: playbook
category: creative-media
source: https://github.com/uxKero/anydesign
license: see upstream repository
args: []
description: >
  Extract a design system from an image, URL, or Figma file into design.md.
  Use when the user wants tokens, components, layout, and rebuild notes.
---

# AnyDesign

## When to Use

Use when GOAL provides a screenshot, website, or Figma file and asks to extract
its design system, recreate a component, or produce a portable `design.md`.

## When NOT to Use

- Do not use to clone a competitor's brand wholesale; extract patterns, not
  trademarks or copyrighted assets.
- Do not use when the user only wants a quick visual opinion.
- Do not use for a Figma file that requires a login the agent does not have.
- Do not invent font names, hex values, or spacing that you cannot see.

## Step Pattern

1. Get the source:
   - URL: `browser_navigate` + `browser_read_full` + a screenshot.
   - Image: `open_file` or `read_document` if it contains text; use
     `screen_read_text` only after the image is visible.
   - Figma: ask for an exported screenshot or a public link.
2. Observe: layout grid, spacing rhythm, type scale, colors, radii, borders,
   shadows, imagery, icon style, and interaction cues.
3. Separate **observed** values from **inferred** values. Mark inferred tokens.
4. Write `design.md`:
   - Overview and design principles
   - Color tokens (hex + role)
   - Typography (family, sizes, weights, line heights)
   - Spacing and radius scale
   - Component inventory with states
   - Layout rules and breakpoints
   - Motion and interaction notes
   - Rebuild notes and open questions
5. If the user asks for one component, write `element.md` with structure,
   classes/tokens, and a rebuild prompt instead of the full system.
6. Save the file and read it back to verify.

## Output Shape

A structured `design.md` or `element.md` in Markdown. Include a confidence
level for inferred values.

## Recovery

- Image is low resolution: say which values are uncertain.
- Figma access denied: ask for an export or a public link.
- Brand assets are copyrighted: describe the pattern and suggest a licensed
  alternative.
- Page uses a canvas/WebGL UI: capture multiple screenshots and note that the
  DOM does not expose the design.
