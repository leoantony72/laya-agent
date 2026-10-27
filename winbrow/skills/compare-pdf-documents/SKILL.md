---
name: "compare-pdf-documents"
skill: "compare_pdf_documents"
args:
  - name: "file_paths"
    type: "string"
    required: true
    default: null
    description: "List of absolute paths to the PDF files to compare."
  - name: "output_folder"
    type: "string"
    required: false
    default: null
    description: "Folder where the comparison summary file should be written. Defaults to the folder of the first file."
description: >
  Compare two or more PDF documents by extracting text and computing pairwise
  similarity and differences. Use when the user asks to compare multiple PDFs
  or find changes between document versions. Do not use for a single PDF, for
  non-document files, or when only a summary of one document is needed.
---

## When to Use

When the user asks to compare multiple PDF documents, find differences between document versions, or check similarity among PDFs.

## When NOT to Use

When comparing a single PDF, comparing non-PDF files, or when the user only wants a summary of one document.

## Step Pattern

1. Resolve the list of PDF paths from file_paths.
2. Extract text from each PDF using read_document or a Python script with pypdf/PyPDF2.
3. Create a Python script that loads all extracted texts, computes pairwise similarity ratios with difflib.SequenceMatcher, and writes a summary of differences to output_folder.
4. Run the script with run_command.
5. Read the generated summary and report pairwise similarities and notable differences.

## Args Detail

- `file_paths` (string, required): List of absolute paths to the PDF files to compare.
- `output_folder` (string, default=None): Folder where the comparison summary file should be written. Defaults to the folder of the first file.

## Output Shape

Returns a result dict with `success`, `output`, and any state fields the
chosen tool provides. A successful step means the tool dispatched the action;
it does not by itself prove the wider goal is complete. Verify with the next
read step before declaring the task done.

## Recovery

If pypdf is unavailable, fall back to PyPDF2 or read_document. If text extraction fails for a file, report that file and continue with the remaining files. If the script errors, inspect the traceback, adjust imports or paths, and rerun.
