# Google Drive

## Operations

- Search: `{"service":"drive","drive_operation":"search","query":"name contains 'report' and trashed = false","limit":20}`
- Upload: `{"service":"drive","drive_operation":"upload","path":"C:\\...\\file.pdf","name":"file.pdf","folder_id":"optional"}`
- Download: `{"service":"drive","drive_operation":"download","file_id":"...","output_path":"C:\\...\\file.pdf"}`

## Rules

- Search first and use the exact file ID.
- Confirm the destination folder before uploading.
- Do not share or change permissions without explicit approval.
- Do not upload confidential files to a personal Drive without consent.
- Verify the returned `webViewLink` or downloaded file size.
