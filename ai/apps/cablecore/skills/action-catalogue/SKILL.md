---
name: action-catalogue
description: The app's main actions by id (create, copy, update, workflow, catalogues, lookup tables, imports, exports) with their risk. Load when the user wants to change data or you look for an operation.
---
# The app's main actions

Each line: `id [risk] — what it does`. Pass the id to `cc_describe` for its exact input, then `cc_action` runs it.
Operations not listed here: `cc_find` searches all 1.5k routes by intent.

{{CATALOGUE}}
