---
name: image-enhancer
skill: playbook_image_enhancer
kind: playbook
category: creative-media
source: https://github.com/ComposioHQ/awesome-claude-skills
license: Apache-2.0
args: []
description: >
  Sharpen, upscale, crop, or batch-enhance images and screenshots. Use before
  a presentation, documentation, or social post.
---

# Image Enhancer

## When to Use

Use when GOAL asks to make an image sharper, clearer, larger, or ready for a
presentation, document, or post. Also use for batch processing a folder.

## When NOT to Use

- Do not use to fabricate detail that is not in the source.
- Do not use for OCR; use `screen-ocr` for text extraction.
- Do not use for video or GIF; use `slack-gif`.
- Do not overwrite the original unless the user explicitly asks.

## Prerequisites

- `run_skill_script` with `skill="image-enhancer"` and
  `script="enhance_image.py"`.
- Pillow is installed.

## Step Pattern

1. Locate the image and inspect its dimensions. If it is a screenshot, keep the
   original untouched.
2. Choose an operation:
   - enhance: `{"operation":"enhance","path":"...","scale":2,"sharpen":1.4,"contrast":1.05,"output_path":"..._enhanced.png"}`
   - resize: `{"operation":"resize","path":"...","width":1920,"output_path":"..."}`
   - crop: `{"operation":"crop","path":"...","box":[0,0,800,600],"output_path":"..."}`
   - batch: `{"operation":"batch","input_dir":"C:\\...\\shots","output_dir":"C:\\...\\enhanced"}`
3. Verify the output dimensions and file size. Preview it when the user asked
   to see it.
4. Report the operations applied. Do not claim lost detail was recovered.

## Parameter Guide

- `scale` above 2× will look soft; prefer a real high-resolution source.
- `sharpen` 1.0 is unchanged; 1.3–1.8 is a reasonable range.
- `unsharp` defaults to true; set false for a soft/artistic image.
- `quality` applies to JPEG/WebP output.

## Output Shape

Returns `success`, `output`, `path` or `paths`, and image dimensions.

## Recovery

- Corrupt image: report the error and ask for another copy.
- Output is larger than expected: check the scale and format.
- Batch finds no images: list the supported extensions and ask for the folder.
- User wants the original replaced: confirm explicitly first.
