"""Per-case cost of an eval run: steps, tool calls, tokens (cached / uncached), time, grade.

    uv run python -m ai.apps.cablecore.evals.scoreboard <results.json> [results.graded.json] [out.json]

Read from the runner's own results (each turn records the loop's usage), so no tracing service is needed.
Uncached input = input - cached: what each step really costs. Cached input is the re-sent prefix.
"""

import json
import sys
from typing import Any

COUNTS = ('steps', 'tool_calls', 'searches', 'skills', 'reasoning_tokens', 'output_tokens', 'cached_tokens')


def score(path: str, graded_path: str | None = None) -> list[dict[str, Any]]:
    with open(path, encoding='utf-8') as f:
        results = json.load(f)
    grades: dict[str, str] = {}
    if graded_path:
        with open(graded_path, encoding='utf-8') as f:
            grades = {r['id']: r['result'] for r in json.load(f)}
    rows = []
    for case in sorted(results, key=lambda r: r['id']):
        turns = case.get('turns') or []
        usage = [t.get('usage') or {} for t in turns]
        row: dict[str, Any] = {'id': case['id'], 'grade': grades.get(case['id'], '')}
        row.update({k: sum(u.get(k, 0) for u in usage) for k in COUNTS})
        row['uncached'] = sum(u.get('input_tokens', 0) - u.get('cached_tokens', 0) for u in usage)
        row['seconds'] = round(sum(t.get('latency_s') or 0 for t in turns), 1)
        rows.append(row)
    return rows


def table(rows: list[dict[str, Any]]) -> str:
    cols = ['id', 'grade', 'steps', 'tool_calls', 'searches', 'skills', 'uncached', 'cached_tokens', 'output_tokens',
            'reasoning_tokens', 'seconds']
    lines = [' | '.join(cols), ' | '.join('---' for _ in cols)]
    lines += [' | '.join(str(r.get(k, 0)) for k in cols) for r in rows]
    total = {k: sum(r.get(k, 0) for r in rows) for k in cols[2:]}
    passed = sum(1 for r in rows if r.get('grade') == 'pass')
    lines.append(' | '.join(['TOTAL', f'{passed} pass'] + [str(round(total[k], 1)) for k in cols[2:]]))
    return '\n'.join(lines)


if __name__ == '__main__':
    result = score(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(table(result))
    if len(sys.argv) > 3:
        with open(sys.argv[3], 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=1)
