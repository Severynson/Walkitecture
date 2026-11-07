from types import SimpleNamespace

import pytest
from openai import APIConnectionError

from app.services import interpreter
from app.services.interpreter import _LLMEdit, interpret_edit, interpret_with_openai, interpret_with_rules


def dumped(result):
    return result.command.model_dump(by_alias=True, exclude_none=True) if result.command else None


@pytest.mark.parametrize(
    ("text", "selected", "expected"),
    [
        (
            "Make the selected window 50% wider.",
            "window_1",
            {"operation": "resize_selected_window", "targetId": "window_1", "parameters": {"factor": 1.5}},
        ),
        (
            "Increase the ceiling height to 3 meters.",
            None,
            {"operation": "set_ceiling_height", "parameters": {"height": 3.0}},
        ),
        ("Raise the ceiling by 30 cm", None, {"operation": "set_ceiling_height", "parameters": {"height": 3.0}}),
        ("Make all windows 20% narrower", None, {"operation": "resize_windows", "parameters": {"factor": 0.8}}),
        (
            "make this window 1.8 m wide",
            "window_2",
            {"operation": "resize_selected_window", "targetId": "window_2", "parameters": {"width": 1.8}},
        ),
        (
            "Make it twice as wide",
            "window_3",
            {"operation": "resize_selected_window", "targetId": "window_3", "parameters": {"factor": 2.0}},
        ),
        (
            "move this wall 40 cm",
            "wall_7",
            {"operation": "move_selected_wall", "targetId": "wall_7", "parameters": {"offset": 0.4}},
        ),
    ],
)
def test_rules_map_common_phrasings(demo_building, text, selected, expected):
    result = interpret_with_rules(text, selected, demo_building)
    assert result.clarification is None
    assert dumped(result) == expected


def test_rules_use_camera_direction_for_back(demo_building):
    # wall_7 runs +x, so its left normal is +y; the camera looks along +y, so "back" is -y.
    result = interpret_with_rules("Move this partition back 30 cm", "wall_7", demo_building, camera_forward=(0, 1))
    assert result.command.parameters.offset == pytest.approx(-0.3)


@pytest.mark.parametrize(
    ("text", "selected", "camera"),
    [
        ("Move this partition back 30 cm", "wall_7", None),  # direction needs a reference frame
        ("Make this window wider", "window_1", None),  # no amount
        ("Make this 50% wider", "room_1", None),  # nothing resizable selected
        ("Paint the walls blue", None, None),  # unsupported
    ],
)
def test_rules_ask_for_clarification(demo_building, text, selected, camera):
    result = interpret_with_rules(text, selected, demo_building, camera)
    assert result.command is None
    assert result.clarification


class _FakeCompletions:
    def __init__(self, parsed=None, refusal=None, error=None):
        self.parsed, self.refusal, self.error, self.calls = parsed, refusal, error, []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        message = SimpleNamespace(parsed=self.parsed, refusal=self.refusal)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _llm(**fields):
    base = dict(target_id=None, factor=None, width=None, height=None, offset=None, explanation="ok", clarification=None)
    return _LLMEdit(**{**base, **fields})


async def test_openai_output_is_converted_to_typed_command(demo_building, settings):
    completions = _FakeCompletions(parsed=_llm(operation="resize_selected_window", target_id="window_1", factor=1.5))
    result = await interpret_with_openai(
        "50% wider", "window_1", demo_building, None, settings, client=_fake_client(completions)
    )
    assert result.source == "openai"
    assert dumped(result)["parameters"] == {"factor": 1.5}
    assert completions.calls[0]["response_format"] is _LLMEdit
    assert '"selectedKind": "window"' in completions.calls[0]["messages"][1]["content"]


async def test_openai_invalid_command_becomes_clarification(demo_building, settings):
    completions = _FakeCompletions(parsed=_llm(operation="set_ceiling_height", height=None))
    result = await interpret_with_openai(
        "raise it", None, demo_building, None, settings, client=_fake_client(completions)
    )
    assert result.command is None and "invalid command" in result.clarification


async def test_openai_none_operation_passes_clarification(demo_building, settings):
    completions = _FakeCompletions(parsed=_llm(operation="none", clarification="Which window?"))
    result = await interpret_with_openai("wider", None, demo_building, None, settings, client=_fake_client(completions))
    assert result.command is None and result.clarification == "Which window?"


async def test_falls_back_to_rules_when_openai_fails(demo_building, settings, monkeypatch):
    async def boom(*args, **kwargs):
        raise APIConnectionError(request=SimpleNamespace(method="POST", url="https://api.openai.com"))

    monkeypatch.setattr(interpreter, "interpret_with_openai", boom)
    keyed = settings.model_copy(update={"openai_api_key": "sk-test"})
    result = await interpret_edit("Set the ceiling to 2.9 m", None, demo_building, keyed)
    assert result.source == "rules"
    assert result.command.parameters.height == pytest.approx(2.9)
    assert result.notes and "rule-based" in result.notes[0]
