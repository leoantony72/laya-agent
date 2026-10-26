---
name: swiftui-design
skill: playbook_swiftui_design
kind: playbook
category: creative-media
source: https://github.com/wholiver/swiftui-design-skill
license: see upstream repository
args: []
description: >
  Design a distinctive SwiftUI interface and generate code. Use for iOS or
  macOS UI direction, layout, typography, and review.
---

# SwiftUI Design

## When to Use

Use when GOAL asks for an iOS/macOS screen, a SwiftUI component, a design
direction, or a critique of an existing SwiftUI layout.

## When NOT to Use

- Do not use for web UI; use `anydesign` or `theme-factory`.
- Do not claim the code was compiled or run; this machine is Windows and has no
  Xcode.
- Do not use generic "AI slop" gradients, glassmorphism, or stock SF Symbol
  walls as a substitute for a design direction.
- Do not copy a competitor's app pixel-for-pixel.

## Design Rules

1. **One idea per screen.** The primary action must be obvious in three
   seconds.
2. **Type carries hierarchy.** Use a maximum of three sizes and two weights.
3. **Color is functional.** One accent, one semantic warning, one success.
4. **Spacing is a scale.** Use 4/8/12/16/24/32; do not eyeball random padding.
5. **Motion explains state.** Animate transitions, not decoration.
6. **Accessibility is a requirement.** Dynamic Type, contrast, tap targets
   ≥44pt, and VoiceOver labels.
7. **Platform-native first.** Prefer `NavigationStack`, `List`, `Form`,
   `TabView`, and SF Symbols over custom imitations.

## Step Pattern

1. Define the screen's job, the user, and the primary action.
2. Pick a design direction in 3–5 adjectives and one reference (optional).
3. Sketch the hierarchy: navigation, header, content, primary action, empty and
   error states.
4. Write SwiftUI code with:
   - a `View` struct per screen
   - `@State`/`@Observable` for local state
   - semantic colors (`Color.primary`, `.secondary`) plus one accent
   - Dynamic Type-friendly fonts (`.title`, `.body`, `.caption`)
   - previews with light/dark and large text
5. Save the code to a `.swift` file and read it back.
6. Review against the seven rules and list what is unverified (build, device,
   accessibility audit).

## Output Shape

A short design direction, the SwiftUI code, and a review checklist. State
clearly that the code was not compiled on this Windows machine.

## Recovery

- User wants a build: explain the Xcode requirement and offer code only.
- Layout breaks at large text: use `ViewThatFits` or a vertical fallback.
- Too many colors: reduce to the accent plus semantic colors.
