"""The thinking-trace details a turn's task rows carry: sources of a web search, a command and its printed output."""

import json

from app.chat.agent import command_label, command_output, result_details, step_details
from app.chat.schemas import TaskDetail


def test_command_output_reads_the_printed_text_even_when_the_preview_is_cut() -> None:
    full = json.dumps(
        {"id": "x", "output": [{"type": "output", "data": "ready\r\n"}, {"data": 'R90 = 0.246 "ohm/km"\r\n'}]}
    )
    assert command_output(full) == 'ready\nR90 = 0.246 "ohm/km"\n'
    assert command_output(full[:-3]) == 'ready\nR90 = 0.246 "ohm/km"\n'
    assert command_output("plain error text") == "plain error text"


def test_details_of_search_and_command_steps() -> None:
    found = json.dumps([{"title": "a", "link": "https://x.com/a"}, {"title": "b", "link": "https://y.org"}])
    assert result_details("search_web", found) == [
        TaskDetail(label="https://x.com/a", meta="source"),
        TaskDetail(label="https://y.org", meta="source"),
    ]
    assert step_details("run_command", {"command": "python x.py"}) == [TaskDetail(label="python x.py", meta="command")]
    assert result_details("cc_get", "{}") == []


def test_command_label_says_what_a_script_does() -> None:
    script = "python3 - <<'PY'\n# Resistance of 95 mm2 Cu at 90 C\nR20 = 0.193\nprint(R20)\nPY"
    assert command_label(script) == "Ran a Python script: Resistance of 95 mm2 Cu at 90 C"
    assert command_label("python - <<'PY'\nprint(1)\nPY") == "Ran a Python script"
    assert command_label("ls -la /home/user/work") == "Ran ls -la /home/user/work"
