from app.chat.agent import _bounded


def test_nul_characters_are_dropped_before_storage() -> None:
    items = [{"type": "function_call_output", "call_id": "1", "output": "PNG\x00\x00data"}]
    assert _bounded(items)[0]["output"] == "PNGdata"
