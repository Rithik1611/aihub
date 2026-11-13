---
name: searching
description: How to find designs by their properties (cores, size, conductor, voltage, layers) reliably - attribute keys from the search index, value formats, and why a search returns nothing or thousands. Load before searching designs by their properties.
---
# Searching designs by their properties

## Attributes first, from the index
- Filter on attribute keys that are in the search index. Get them with `cc_attributes` (words for the property, plus your synonyms), never from memory: the app's names differ from everyday terms and from one product class to another. Use a key as `cc_attributes` returns it; keys are case-insensitive.
- The same property can live on several processes, one per product class (e.g. an `INSULATING` and an `INSULATING_MV` key). When `cc_attributes` returns several keys for one property, search each one that applies, or say which product classes you covered.
- Match the stored value form: `cc_attributes` shows the most common stored values (e.g. `CU` not `copper`, `0.6/1` not `0.6/1 kV`, `95` not `95 mm²`). Quote text values with spaces or slashes: `voltage="0.6/1"`.
- Same filters give the same results in any order; the search is deterministic.

## The description is a fallback
A design's `description` reads like `0.6/1kV 3CX95mm² CU C2/PVC-ORANGE`, but it is written when the design is created and may be outdated. Use `description like %…%` only for a property that has no attribute with values, and say so. When you do, confirm the matches against attribute values (`fields`) where they exist.

## Precision
- Attribute filters are precise. Free words are fuzzy text over many fields: `PVC` or `60502-1` alone match most of the database (totals in the thousands). Turn them into attribute filters.
- `like` patterns take no quotes: `description like %XLPE%`, not `'%XLPE%'`.

## When nothing matches
cc_search then lists each filter's count on its own. A filter with a key the index does not have is flagged (with similar keys); a key that exists but matched nothing comes with its most common values. Fix that filter. If every filter matches alone but not together, no design has that combination: say so, and offer the closest designs (relax one filter at a time, the least important to the user first).

## Comparing many designs
`fields` adds attribute values to every row (keys as in `cc_attributes`); with at most 300 matches every page is read in one call. More than 300: add filters first.

## Query forms
`key=value`, `key="text value"`, `key>=50`, `key between 16 and 25`, `key in (95,120)`, `key is null`, `key is not null`, `PROCESS.key=value`, `description like %…%`, `designId contains LV`.
