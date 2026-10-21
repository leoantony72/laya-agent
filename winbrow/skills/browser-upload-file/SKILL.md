---
name: browser-upload-file
skill: browser_upload_file
args:
  - name: file_path
    type: string
    required: true
    description: Absolute path of the file to upload, e.g. C:\Docs\report.pptx.
description: >
  Set an absolute file path on the active page's file input via CDP, bypassing
  the OS file picker. Use for every upload, always after browser-interact
  clicks the button that opens file selection. Do not use it before the trigger
  is clicked, or to type a path into a text field.
---

## When to Use

Use for every file upload: Aspose, Google Drive, OneDrive, Dropbox, forms and
any site with an upload control. It is the only correct way to supply a local
file because it attaches the file directly to the page's file input.

## When NOT to Use

- Do not call it before clicking the button or menu item that reveals the file
  input; the input often does not exist until then.
- Do not type or paste the path into a visible text box.
- Do not use it to download a file; use download-file.
- Do not assume an upload succeeded because browser-interact returned
  success; always wait and read the page.
- Do not upload a file larger than 10 MB to Aspose; use Google Drive instead.

## Step Pattern

Use this exact six-step sequence every time:

1. browser-navigate to the upload page.
2. browser-read-page to understand the UI.
3. browser-interact to click the button that opens file selection.
4. browser-upload-file with the absolute `file_path`.
5. browser-wait for the upload or progress bar to finish.
6. browser-read-page to confirm the success message or resulting view.

Google Drive sequence:

1. browser-interact clicks the "+ New" button.
2. browser-interact clicks "File upload" in the dropdown.
3. browser-upload-file sets the absolute path.
4. browser-wait and browser-read-page confirm the item appears.

Online PPTX viewers, first option that works:

- Option 1: Aspose viewer (aspose.app) for files up to 10 MB.
- Option 2: Google Drive for larger files or when Aspose fails.
- If find-files reports a file larger than 10 MB, skip Aspose entirely.

## Args Detail

- `file_path` must be absolute and must already exist. Resolve it with
  find-files first; never pass a relative name or a `~` path.
- The file input not found error means the upload trigger was not clicked yet.
  Go back to step 3, click the trigger, then retry the upload once.
- The file picker dialog is never used; this skill talks to the page directly.

## Output Shape

Returns `success`, `output`, `url`, `title`, and optional `_warning`. The output
names the file that was set and the input it was attached to. Some sites show
the upload only after a progress bar; a successful set does not prove the
server accepted the file.

## Recovery

If the input is missing, click the upload trigger again and retry once. If the
site rejects the file type or size, choose the alternate viewer or clarify. If
the upload stalls, browser-wait once and read the page; if there is still no
confirmation, do not claim success. Follow the self-correct rule and never
repeat the identical failing upload more than twice.
