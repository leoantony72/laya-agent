---
name: "compare-products-or-brands"
skill: "compare_products_or_brands"
args:
  - name: "entity_a"
    type: "string"
    required: true
    default: null
    description: "The first product, brand, or model to compare."
  - name: "entity_b"
    type: "string"
    required: true
    default: null
    description: "The second product, brand, or model to compare."
  - name: "focus"
    type: "string"
    required: false
    default: null
    description: "Specific aspects to compare, e.g., 'specs, price, battery life'. If omitted, a general comparison is performed."
description: >
  Use this skill to compare two products, brands, or models by searching for
  comparison articles and extracting key specifications, prices, and reviews.
  Provide the two entities to compare and optionally the aspects to focus on.
  This skill is not for comparing documents or PDFs.
---

## When to Use

When the user asks to compare two products, brands, models, or services, and expects a structured comparison of features, prices, or performance.

## When NOT to Use

Do not use for comparing documents, PDFs, or files. Do not use when the user only wants information about a single product without a comparison.

## Step Pattern

1. Construct a search query: 'latest {entity_a} vs latest {entity_b} comparison' plus optional focus (e.g., ' specs, price, battery life').
2. Perform a web search with that query.
3. Select the top 2-3 relevant results (prefer comparison sites, official specs pages, or review sites).
4. For each selected URL, fetch the page content with a focus on the requested aspects.
5. Extract key differences and similarities from the fetched content.
6. Summarize the comparison in a structured format (e.g., table or bullet points).

## Args Detail

- `entity_a` (string, required): The first product, brand, or model to compare.
- `entity_b` (string, required): The second product, brand, or model to compare.
- `focus` (string, default=None): Specific aspects to compare, e.g., 'specs, price, battery life'. If omitted, a general comparison is performed.

## Output Shape

Returns a result dict with `success`, `output`, and any state fields the
chosen tool provides. A successful step means the tool dispatched the action;
it does not by itself prove the wider goal is complete. Verify with the next
read step before declaring the task done.

## Recovery

If a fetch fails (e.g., HTTP 403 or 404), try the next result from the search. If no results are found, refine the search query by adding more specific terms like the current year or the focus aspects. If all fetches fail, report the failure and suggest alternative search terms.
