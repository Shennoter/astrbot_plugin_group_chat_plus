"""Execute the real proactive provider-call boundary without starting timers."""

import ast
import asyncio
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(
    params=[
        "utils/proactive_chat_manager.py",
        "private_chat/private_chat_utils/private_chat_proactive_chat_manager.py",
    ]
)
def invoke(request):
    path = Path(__file__).parents[1] / request.param
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Load the complete setup and call from the production generation try block.
    for block in ast.walk(tree):
        if not isinstance(block, ast.Try):
            continue
        for index, node in enumerate(block.body):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "completion_result"
                for target in node.targets
            ):
                body = block.body[: index + 1]
                body.append(
                    ast.Return(value=ast.Name(id="completion_result", ctx=ast.Load()))
                )
                wrapper = ast.parse("async def invoke(provider, req): pass").body[0]
                wrapper.body = body
                module = ast.fix_missing_locations(
                    ast.Module(body=[wrapper], type_ignores=[])
                )
                namespace = {"time": time}
                exec(compile(module, str(path), "exec"), namespace)  # noqa: S102 -- Checked-in code only.
                return namespace["invoke"]
    pytest.fail("Proactive generation call was not found")


@pytest.mark.parametrize("parts", [None, [], [object()]])
def test_extra_parts_reach_proactive_provider(invoke, parts):
    req = types.SimpleNamespace(
        prompt="history and current task",
        session_id="test",
        contexts=[],
        system_prompt="fixed persona and rules",
        image_urls=["test-image"],
    )
    if parts is not None:
        req.extra_user_content_parts = parts
    provider = types.SimpleNamespace(text_chat=AsyncMock(return_value="result"))
    assert asyncio.run(invoke(provider, req)) == "result"
    kwargs = provider.text_chat.call_args.kwargs
    assert kwargs["prompt"] == req.prompt
    assert kwargs["system_prompt"] == req.system_prompt
    assert kwargs["image_urls"] == req.image_urls
    assert kwargs["func_tool_manager"] is None
    if parts:
        assert kwargs["extra_user_content_parts"] is parts
    else:
        assert "extra_user_content_parts" not in kwargs
