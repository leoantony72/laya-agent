---
name: pixelbin-media
skill: playbook_pixelbin_media
kind: playbook
category: creative-media
source: https://github.com/anandpareek-hub/pixelbin-claude-skill
license: see upstream repository
args: []
description: >
  Generate, transform, and deliver media through Pixelbin. Use for bulk image
  edits, CDN delivery, and asset pipelines when an API key is configured.
---

# Pixelbin Media

## When to Use

Use when GOAL asks for bulk image processing, background removal, upscaling,
watermarking, CDN URLs, or a Pixelbin-backed media pipeline.

## When NOT to Use

- Do not use without a Pixelbin account and API key.
- Do not upload confidential images to a third-party service without consent.
- Do not guess an endpoint or transformation string; use the Pixelbin dashboard
  docs or the user's own URL template.
- Do not promise a specific transformation result without checking the output.

## Prerequisites

- Pixelbin API key in Integrations or `PIXELBIN_API_KEY`.
- Optional: cloud name and zone for CDN transforms.
- `run_skill_script` with `skill="pixelbin-media"` and `script="pixelbin.py"`.
- `requests` is installed.

## Step Pattern

1. If available, call `integration_status("pixelbin")`; stop and ask the user
   to configure it if missing.
2. Confirm the source file, the intended transformation, and the output path.
3. Choose one operation:
   - upload: `{"operation":"upload","path":"C:\\...\\photo.png","remote_path":"campaign/photo.png"}`
   - transform: `{"operation":"transform","cloud":"...","zone":"...","transformations":"t.resize(w:1200)/t.compress(q:80)","remote_path":"campaign/photo.png","output_path":"C:\\...\\out.png"}`
   - API: `{"operation":"request","method":"GET","endpoint":"/v1/...","params":{...}}`
4. Verify the returned path or JSON. For a transform, check the byte size and
   dimensions; do not assume the CDN string was valid.
5. Report the remote URL, local path, and any cost/credit implication.

## Spec Detail

- `base_url` defaults to `https://api.pixelbin.io`; override only if the user
  has a regional endpoint.
- `endpoint` must start with `/` and must not contain `..`.
- `transformations` is the exact Pixelbin transformation path; the skill does
  not invent it.
- Credentials are never written to a file or returned in the output.

## Output Shape

Returns `success`, `path` for downloads, and `data` with the API response or
CDN URL.

## Recovery

- 401/403: invalid key; ask the user to re-enter it.
- 404 on an endpoint: ask for the exact API path from the Pixelbin dashboard.
- Transform URL returns an error image: check the transformation syntax.
- Credit/quota error: report it; do not retry in a loop.
