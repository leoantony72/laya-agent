# Tabular Review

## Goal

Review many documents against one consistent rubric and produce a comparison
table a human can audit.

## Method

1. Define the columns before reading. Typical columns:
   - document name, date, parties, document type
   - key clause present? (yes/no/partial)
   - risk level (green/amber/red)
   - unusual terms
   - missing information
   - reviewer confidence (high/medium/low)
2. Read each document with `read_document`. Keep a short evidence quote for
   every non-obvious cell.
3. Fill one row per document. Use `unknown` rather than guessing.
4. Sort by risk, then by date.
5. Add a summary of the most common issues and the outliers.

## Output Format

Markdown table, followed by:
- **Common findings**
- **Outliers requiring human review**
- **Documents that could not be read**
- **Rubric** (exact definitions used for green/amber/red)

## Rules

- Do not compare documents that use different rubrics without saying so.
- If the set is large, state the sample and how it was chosen.
- Never summarize a document you did not read.
- Keep evidence quotes short; link to the file path for the full text.
