---
name: "upload-file-to-google-drive"
skill: "upload_file_to_google_drive"
args:
  - name: "file_path"
    type: "string"
    required: true
    default: null
    description: "Absolute path to the local file to upload."
  - name: "destination_folder"
    type: "string"
    required: false
    default: null
    description: "Google Drive folder name or path to upload into; if omitted, upload to My Drive root."
description: >
  Uploads a local file to Google Drive. Use when the user asks to upload a
  file from their computer to Google Drive. Do not use for uploading to other
  cloud services or for downloading files from Drive.
---

## When to Use

User wants to upload a local file to Google Drive, either to My Drive root or a specified folder.

## When NOT to Use

User wants to upload to a different cloud service, share an existing Drive file, or download from Drive.

## Step Pattern

1. Navigate to https://drive.google.com/drive/my-drive, or to the destination_folder if provided.
2. Click the 'New' button.
3. Use the accessibility tree to find and click the 'File upload' menu item.
4. Call browser_upload_file with file_path to set the file input.
5. Call browser_wait for a few seconds to allow the upload to complete.
6. Verify by navigating to https://drive.google.com/drive/search?q=<file base name> or by reading the page to confirm the file appears.

## Args Detail

- `file_path` (string, required): Absolute path to the local file to upload.
- `destination_folder` (string, default=None): Google Drive folder name or path to upload into; if omitted, upload to My Drive root.

## Output Shape

Returns a result dict with `success`, `output`, and any state fields the
chosen tool provides. A successful step means the tool dispatched the action;
it does not by itself prove the wider goal is complete. Verify with the next
read step before declaring the task done.

## Recovery

If the upload menu is not found, refresh the page or use the keyboard shortcut Alt+C then U. If the file input is not found, ensure the file picker opened and retry. If upload fails, check that file_path exists and is readable. If verification fails, wait longer and re-search.
