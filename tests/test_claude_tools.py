"""Unit tests for ai/tools.py and ai/client.py."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# handle_save_script tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_script_writes_to_setting_subdir(tmp_path: Path):
    from claude_feishu_flow.ai.tools import handle_save_script

    code = "print('hello')\n"
    exp_dir = tmp_path / "exp_abc"
    result = await handle_save_script(
        {"filename": "main.py", "code": code},
        experiment_dir=exp_dir,
    )

    saved = Path(result)
    assert saved.exists()
    assert saved.read_text() == code
    assert saved.name == "main.py"
    # File must be inside setting/ subdirectory
    assert saved.parent.name == "setting"
    assert "exp_abc" in result


@pytest.mark.asyncio
async def test_save_script_creates_setting_dir(tmp_path: Path):
    from claude_feishu_flow.ai.tools import handle_save_script

    exp_dir = tmp_path / "new_exp" / "nested"
    assert not exp_dir.exists()

    await handle_save_script({"filename": "plan.md", "code": "# plan\n"}, exp_dir)

    assert (exp_dir / "setting").exists()
    assert (exp_dir / "setting" / "plan.md").exists()


@pytest.mark.asyncio
async def test_save_script_returns_absolute_path(tmp_path: Path):
    from claude_feishu_flow.ai.tools import handle_save_script

    result = await handle_save_script(
        {"filename": "main.py", "code": "pass\n"},
        experiment_dir=tmp_path,
    )
    assert Path(result).is_absolute()


# ---------------------------------------------------------------------------
# ClaudeClient.generate_experiment tests
# (mock anthropic.AsyncAnthropic so no real API calls)
# ---------------------------------------------------------------------------

def _make_tool_use_block(tool_use_id: str, filename: str, code: str):
    """Build a fake tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_use_id
    block.name = "save_script"
    block.input = {"filename": filename, "code": code}
    return block


def _make_text_block(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_response(stop_reason: str, content: list):
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = content
    return resp


@pytest.mark.asyncio
async def test_generate_experiment_saves_main_py(tmp_path: Path):
    """Claude saves plan.md then main.py → main.py path returned."""
    from claude_feishu_flow.ai.client import ClaudeClient

    # Round 1: Claude saves plan.md
    round1 = _make_response("tool_use", [
        _make_tool_use_block("tu_001", "plan.md", "# plan\n"),
    ])
    # Round 2: Claude saves main.py
    round2 = _make_response("tool_use", [
        _make_tool_use_block("tu_002", "main.py", "print('hi')\n"),
    ])

    mock_create = AsyncMock(side_effect=[round1, round2])

    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        instance = MockAnthropic.return_value
        instance.messages = MagicMock()
        instance.messages.create = mock_create

        client = ClaudeClient(api_key="test-key")
        workspace = tmp_path / "exp_test"
        result = await client.generate_experiment("write hello script", workspace)

    assert Path(result).name == "main.py"
    assert (workspace / "setting" / "main.py").read_text() == "print('hi')\n"
    assert (workspace / "setting" / "plan.md").read_text() == "# plan\n"


@pytest.mark.asyncio
async def test_generate_experiment_only_main_py(tmp_path: Path):
    """Claude skips plan.md and only saves main.py — still completes successfully."""
    from claude_feishu_flow.ai.client import ClaudeClient

    round1 = _make_response("tool_use", [
        _make_tool_use_block("tu_003", "main.py", "print('world')\n"),
    ])

    mock_create = AsyncMock(return_value=round1)

    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        instance = MockAnthropic.return_value
        instance.messages = MagicMock()
        instance.messages.create = mock_create

        client = ClaudeClient(api_key="test-key")
        workspace = tmp_path / "exp_only_main"
        result = await client.generate_experiment("write world script", workspace)

    assert (workspace / "setting" / "main.py").read_text() == "print('world')\n"


@pytest.mark.asyncio
async def test_generate_experiment_handles_text_then_tool(tmp_path: Path):
    """Claude emits text block then tool_use in same response — both handled."""
    from claude_feishu_flow.ai.client import ClaudeClient

    round1 = _make_response("tool_use", [
        _make_text_block("Here is the script:"),
        _make_tool_use_block("tu_004", "main.py", "print('world')\n"),
    ])

    mock_create = AsyncMock(return_value=round1)

    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        instance = MockAnthropic.return_value
        instance.messages = MagicMock()
        instance.messages.create = mock_create

        client = ClaudeClient(api_key="test-key")
        workspace = tmp_path / "exp_text_tool"
        result = await client.generate_experiment("write world script", workspace)

    assert (workspace / "setting" / "main.py").read_text() == "print('world')\n"


@pytest.mark.asyncio
async def test_generate_experiment_raises_if_no_main_py(tmp_path: Path):
    """RuntimeError raised when Claude never saves main.py."""
    from claude_feishu_flow.ai.client import ClaudeClient

    # Claude always returns end_turn with only text
    end_turn = _make_response("end_turn", [_make_text_block("I cannot help.")])
    mock_create = AsyncMock(return_value=end_turn)

    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        instance = MockAnthropic.return_value
        instance.messages = MagicMock()
        instance.messages.create = mock_create

        client = ClaudeClient(api_key="test-key")
        with pytest.raises(RuntimeError, match="did not save main.py"):
            await client.generate_experiment("do nothing", tmp_path / "t")


@pytest.mark.asyncio
async def test_generate_experiment_system_prompt_included(tmp_path: Path):
    """The system prompt is passed to the API call."""
    from claude_feishu_flow.ai.client import ClaudeClient
    from claude_feishu_flow.ai.prompt import build_system_prompt

    round1 = _make_response("tool_use", [
        _make_tool_use_block("tu_005", "main.py", "x=1\n"),
    ])
    mock_create = AsyncMock(return_value=round1)

    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        instance = MockAnthropic.return_value
        instance.messages = MagicMock()
        instance.messages.create = mock_create

        client = ClaudeClient(api_key="test-key")
        await client.generate_experiment("test", tmp_path / "t2")

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["system"] == build_system_prompt()


@pytest.mark.asyncio
async def test_generate_experiment_tools_schema_passed(tmp_path: Path):
    """The save_script tool schema is included in every API call."""
    from claude_feishu_flow.ai.client import ClaudeClient
    from claude_feishu_flow.ai.tools import ALL_TOOLS

    round1 = _make_response("tool_use", [
        _make_tool_use_block("tu_006", "main.py", "pass\n"),
    ])
    mock_create = AsyncMock(return_value=round1)

    with patch("anthropic.AsyncAnthropic") as MockAnthropic:
        instance = MockAnthropic.return_value
        instance.messages = MagicMock()
        instance.messages.create = mock_create

        client = ClaudeClient(api_key="test-key")
        await client.generate_experiment("test", tmp_path / "t3")

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["tools"] == ALL_TOOLS
