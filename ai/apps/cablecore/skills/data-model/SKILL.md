---
name: data-model
description: How the main tables link (foreign keys) and what their values mean - formulas, costing, history, tenders. Load before reading tables with cc_related or SQL, or when explaining where a value comes from.
---
# Data map (relations of the most used models, from the schema)

{{DATA_MAP}}

# What the values mean
- Formulas are stored per attribute kind: `FormulaProcessAttributes` (process attributes), `FormulaPlantHeaders` (plant attributes such as noWires, wireDiam, conductorCSA), `FormulaCableHeaders`, `FormulaBomAttributes`, `FormulaResourceAttributes`; rows are chosen by plant and productClassification, with `ALL` as the fallback. A formula reading `plant.X` gets the value of X's FormulaPlantHeaders formula, and `layer.P.X` gets process P's X.
- Each stored attribute value links the formula that computed it (`formulaProcessAttributesId`, `formulaPlantHeadersId`, `formulaCableHeadersId`, `formulaBomAttributesId`; empty: no formula). Otherwise the formula that applies is the row for that attribute (and process) that best matches the design's productClassification, then its plant, with `ALL` matching any.
- `cableCORE.vlookup(table, …, key, columnIndex, …)` and `cableCORE.vlookupByHeader(table, segment, type, key, header, plant, byRange)` return the cell of the row whose first column equals `key` (with `byRange` true, the nearest lower row when none is equal), or '' when no row matches. Table data is `LookupTable.values`: rows as JSON objects, the first column usually `Column_1`.
- Costing: the cost sheet view totals only the cost groups placed on the costing sheet; the stored roll-up (`CostingVersionRollupBreakdown`, level 0 row) includes every cost, so the two can differ. Say which one you report.
- `DesignHistory.changeType` is MANUAL (a user's edit) or SYSTEM_CALCULATED (recalculated by a formula); a row without it does not say which.
- Tenders are listed for the user's active plant only.
