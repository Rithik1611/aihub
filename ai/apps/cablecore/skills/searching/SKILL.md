---
name: searching
description: How designs are found by construction (cores, size, conductor, voltage, layers), value formats, and why a search returns nothing or thousands. Load before searching designs by their properties.
---
# Searching designs

## What the data holds
- A design's `description` encodes its construction: `<voltage> <cores>CX<size>mm² <conductor> <class>/<layers>-<colour>`, e.g. `0.6/1kV 3CX95mm² CU C2/PVC-ORANGE`, `19/33kV 3CX95mm²/50mm² AL C2/XLPE/CWS/PVC BLACK`.
- Core count is **not** a filled attribute (`core` exists but is empty; `noOfConductors` is mostly 1). Find cores through the description: `description like %3CX95%` (3 cores × 95 mm²), `description like %2CX%` (any 2-core).
- Reliable attribute filters: `conductorCSA` (number, mm²), `conductorType` (`CU`, `AL`), `voltage` (quoted text: `voltage="0.6/1"`, `voltage="19/33"`), `armourMaterial` (quoted text).
- Process attributes take the design's own process names, which depend on the product class (insulation is `INSULATING` in Low Voltage, `INSULATING_MV` in Medium Voltage). Read them from one matching design with `cc_get` before filtering on them.

## Precision
- Attribute filters and `description like %…%` are precise.
- Free words are fuzzy text over many fields: `PVC` or `60502-1` alone match most of the database (totals in the thousands). Combine them with attribute filters, or use `description like`.
- `like` patterns take no quotes: `description like %XLPE%`, not `'%XLPE%'`.

## When nothing matches
cc_search answers a zero-match search with each filter's count on its own and flags field names that do not exist (with similar names). Drop or fix the filter that matches nothing alone; if every filter matches alone but not together, no design has that combination: say so, and offer the closest designs (relax one filter at a time, starting with the least important to the user).

## Comparing many designs
`fields` adds attribute values to every row; with at most 300 matches every page is read in one call. More than 300: add filters first.

## Examples (checked against the live search)
- 3-core 95 mm² copper, 0.6/1 kV: `conductorType=CU voltage="0.6/1" description like %3CX95%`
- any aluminium 240 mm²: `conductorType=AL conductorCSA=240`
- size range: `conductorCSA between 16 and 25`, list: `conductorCSA in (95,120)`
- by stranding: `STRANDING.conductorCSA=300`
- missing standard: `referenceStandard is null`
