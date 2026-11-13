---
name: chat-output
description: How to draw a chart in the chat panel and give the user a file (download links). Load before answering with a chart or a file.
---
# What the chat panel shows

Markdown: headings, lists, bold, links, code, tables. A chart is drawn from a fenced block tagged `chart` that contains JSON:
`{"type": "bar" | "line" | "area" | "scatter" | "pie", "title": "...", "x": "<key>", "y": ["<key>", ...], "data": [{"<key>": value, ...}], "xLabel": "...", "yLabel": "..."}` (pie: `x` is the label, the first `y` the value).

To give the user a file, write it under `~/work/{{USER_EMAIL}}/` and link it as `[file name](/sandbox/home/user/work/...)` with its full path. A route that returns a file (an export) comes back from `cc_action` with a `download_link`: give the user `[file name](download_link)`. A file you have not linked is not something the user has. Files the user attaches are in the sandbox; the message says where (`[Attached file "<name>" is at <path>]`).
