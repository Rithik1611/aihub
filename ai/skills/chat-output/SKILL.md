---
name: chat-output
description: How to draw a chart in the chat panel and give the user a file (download links). Load before answering with a chart or a file.
---
# What the chat panel shows

Markdown: headings, lists, bold, links, code, tables. A chart is drawn from a fenced block tagged `chart` that contains JSON:
`{"type": "bar" | "line" | "area" | "scatter" | "pie", "title": "...", "x": "<key>", "y": ["<key>", ...], "data": [{"<key>": value, ...}], "xLabel": "...", "yLabel": "...", "unit": "...", "stacked": false}`.
- `x` names the category or x value of each row; `y` the series (up to 8), each a number per row. The keys are shown to the user as axis and series names, so use readable words ("Cost per lot", "Copper"), not field names (costPerLotSize). Values are numbers, not strings with units; the unit goes in `unit` (e.g. "mm²", "Ω/km", "kg/km").
- One y scale per chart: two measures of different size or unit are two charts, never one.
- The title and axis say what is counted and for what scope ("Plant 1300 designs (distinct codes) per classification"); the parts add up to the total you state in the text.
- pie (drawn as a donut): `x` is the slice name, the first `y` its value; it suits a few parts of one whole (more than 6 slices fold into "Other"). `stacked` stacks bar or area series that add up to a total. Long or many bar labels are drawn as horizontal bars by themselves.
- The user can hover for values, switch to a table, hide series, download PNG/CSV and enlarge the chart, so do not repeat the same numbers as a table under it.

To give the user a file, write it under `~/work/` (the user's own sandbox home) and link it as `[file name](/sandbox<full absolute path>)`, e.g. `/sandbox/home/ab12cd34/work/report.xlsx` (`realpath` gives the path). A route that returns a file (an export) comes back from `cc_action` with a `download_link`: give the user `[file name](download_link)`. A file you have not linked is not something the user has. Files the user attaches are in the sandbox; the message says where (`[Attached file "<name>" is at <path>]`).
