---
name: searching
description: How to find designs by their properties (cores, size, conductor, voltage, layers) with exact counts - qualified attribute keys from the search index, versions, value formats, and why a search returns nothing or thousands. Load before searching designs by their properties.
---
# Searching designs by their properties

## Keys: always qualified, always from the index
- Get keys with `cc_attributes` (words for the property plus your synonyms) and use them exactly as returned: `plant.<key>` for a design-level attribute, `PROCESS.<key>` for a process attribute. Never from memory, never unprefixed: an unprefixed key matches the attribute in **any** process, and a template default in one process (e.g. `LAYUP.conductorCSA` = 25 on 98% of designs) then makes the count wrong.
- `cc_attributes` flags keys where one value covers most designs (`warning`: probably a template default). Do not filter on those for a real property; take the `plant.` key or another process.
- Match the stored value form shown in `common_values`: `CU` not `copper`, `0.6/1` not `0.6/1 kV`, `95` not `95 mm²`. Quote text with spaces or slashes: `plant.voltage="0.6/1"`.

## One property, several keys
The same property can live in different keys by plant or product class: 1300 stores conductor size in `plant.conductorCSA`, Demo's conductor designs in `STRANDING.conductorCSA`; insulation is `INSULATING` (LV) or `INSULATING_MV` (MV). When `cc_attributes` returns several real keys for one property, search each and report the designs per key; say which keys you covered.

## Counts
- The search index holds **every version** of a design. `cc_search` de-duplicates rows to the latest version and reports `designs` (distinct designs) next to `rows_in_index`. Report `designs`.
- `plant.<key> is null` matches only designs that have the attribute with an empty value; designs without the attribute at all are not counted. Say so when you report it.

## The description is a fallback
A design's `description` reads like `0.6/1kV 3CX95mm² CU C2/PVC-ORANGE`, written when the design is created; it may be outdated. Use `description like %…%` only for a property with no attribute, say so, and confirm matches against attribute values (`fields`).

## When nothing matches
cc_search lists each filter's count on its own, flags keys the index does not have (with similar keys), and shows a key's common values when the key exists but the value does not. Fix that filter. If every filter matches alone but not together, no design has that combination: say so and offer the closest designs (relax one filter at a time, the least important to the user first).

## Comparing many designs
`fields` adds attribute values to every row (keys as in `cc_attributes`); with at most 300 matches every page is read in one call. More than 300: add filters first.

## Query forms
`plant.key=value`, `plant.key="text value"`, `plant.key>=50`, `plant.key between 16 and 25`, `plant.key in (95,120)`, `plant.key is null`, `PROCESS.key=value`, `description like %…%`, `designId=LV-7034`, `designId contains LV`, `enquiryId=ENQ-0214`, `customerName="Cable_World"`, `status=Draft`. Same filters in any order give the same result.
