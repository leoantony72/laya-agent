---
name: algorithmic-art
skill: playbook_algorithmic_art
kind: playbook
category: creative-media
source: https://github.com/anthropics/skills/tree/main/skills/algorithmic-art
license: See LICENSE.txt (upstream terms, reference only)
args: []
description: >
  Create original p5.js generative art with seeded randomness and an interactive viewer.
---

# Algorithmic Art

## When to Use

Use when GOAL asks for generative art, algorithmic art, code art, a flow field,
particle system, procedural pattern, or "art made with code". Also use for an
interactive p5.js artwork the user can explore and download.

## When NOT to Use

- Do not use for a static poster or infographic; use `canvas-design`.
- Do not use for photo editing, image generation APIs, or video.
- Do not copy an existing artist's signature work. Make an original system.
- Do not browse image sites for "inspiration" and reproduce their output.

## Two Phases

1. **Algorithmic philosophy** — a 4–6 paragraph manifesto for a computational
   aesthetic movement.
2. **Implementation** — express that philosophy as a p5.js system with seeded
   randomness and tunable parameters.

Beauty must come from the process: the same code with a different seed should
produce a different but equally composed artwork.

## Step Pattern

1. Read the user's request for a subtle conceptual thread. Do not illustrate it
   literally; embed it in parameters, forces, or emergent behavior.
2. Write the philosophy (name + 4–6 paragraphs) into `philosophy.md`.
3. Scaffold the folder:

   ```json
   {"operation":"none","output_dir":"C:\\Users\\me\\Desktop\\flow-field",
    "title":"Organic Turbulence","philosophy":"<the full text>"}
   ```

   Call it with `run_skill_script(skill="algorithmic-art",
   script="scaffold_art.py", spec=<the JSON>)`.
   It creates `index.html` (viewer), `sketch.js` (placeholder), and
   `philosophy.md`.
4. Overwrite `sketch.js` with the real algorithm using `create_text_file`.
   Never rewrite `index.html`; it is the fixed viewer.
5. Overwrite `philosophy.md` with the final text.
6. `open_file(index.html)` and describe the system to the user. If the page is
   blank, fix `sketch.js` (syntax/runtime error), not the viewer.
7. Offer the seed controls, sliders, and PNG download.

## sketch.js Contract

The viewer expects these globals. Keep the names exact.

```javascript
const artTitle = "Organic Turbulence";

const params = {
  seed: 12345,
  particleCount: 6000,
  noiseScale: 0.004,
  trail: 0.92,
  palette: ["#d97757", "#6a9bcc", "#788c5d", "#b0aea5"]
};

const defaultParams = { ...params };

const parameterSpec = [
  { key: "particleCount", label: "Particles", min: 500, max: 20000, step: 100 },
  { key: "noiseScale", label: "Noise scale", min: 0.001, max: 0.02, step: 0.001 },
  { key: "trail", label: "Trail", min: 0.80, max: 0.99, step: 0.01 }
];

function setup() {
  const canvas = createCanvas(900, 900);
  canvas.parent("canvas-container");
  randomSeed(params.seed);
  noiseSeed(params.seed);
  resetArt();
}

function draw() {
  // the generative system
}

function resetArt() {
  // clear state, re-seed, rebuild particles / fields
  background(250, 249, 245);
  randomSeed(params.seed);
  noiseSeed(params.seed);
}
```

Rules:

- `parameterSpec` must match `params` keys; the viewer builds the sliders.
- Re-seed inside `resetArt()` so Prev/Next/Random and Regenerate reproduce.
- Use `noLoop()` for a still composition, or animate with a bounded frame rate.
- Draw into a 900×900 canvas; the viewer scales it responsively.
- Keep the palette intentional (3–5 colors), not random RGB.
- Avoid external assets and network calls; p5.js from the CDN is the only
  dependency.

## Craftsmanship

- **Controlled chaos:** randomness must be constrained by rules (noise fields,
  forces, ratios, thresholds), not pure noise.
- **Composition:** maintain a focal region, balance density, and let negative
  space exist. The first frame should already look composed.
- **Color:** derive color from system state (velocity, density, age), not from
  a random picker per element.
- **Performance:** 60 fps on a laptop for animation; cap particle counts and
  avoid per-frame allocations where possible.
- **Reproducibility:** same seed + same parameters = identical output.

## Verification

- `open_file(index.html)`; the canvas must render without console errors.
- Change one slider and confirm the artwork responds.
- Prev/Next seed must produce a different composition.
- Download PNG must save a file.

## Recovery

- Blank canvas: syntax error in `sketch.js` or `params` key missing. Fix the
  script; do not edit the viewer.
- Controls do nothing: `parameterSpec` key does not match a `params` key.
- Seed changes but the art does not: `resetArt()` does not re-seed/rebuild.
- Scaffold refuses to overwrite: create a new folder or ask before passing
  `overwrite=true`.
