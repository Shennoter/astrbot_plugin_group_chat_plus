"""Exercise real prompt construction without starting AstrBot or calling a model.

The request hook is compiled from its complete production method because importing
main.py registers all platform handlers and imports the web server. Platform and
unrelated injection services are doubles; the prompt builder and rewriter are real.
"""

import ast
import asyncio
import importlib.util
import logging
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = "group_chat_plus_cache_test_package"
HISTORY = "=== 历史消息上下文 ===\n" + (
    "Alice(ID:101): 之前的问题\nBob(ID:202): 之前的补充\n" * 100
)


class Request(types.SimpleNamespace):
    def __init__(self, **kwargs):
        super().__init__(
            **{
                "prompt": "",
                "system_prompt": "",
                "contexts": [],
                "image_urls": [],
                "audio_urls": [],
                "func_tool": None,
                **kwargs,
            }
        )


class Event:
    session_id = "group-test"
    unified_msg_origin = "test:GroupMessage:group-test"

    def __init__(self, sender="Alice", sender_id="101", message="这件事怎么处理？"):
        self.sender, self.sender_id, self.message = sender, sender_id, message
        self.extras = {}

    def get_sender_name(self):
        return self.sender

    def get_sender_id(self):
        return self.sender_id

    def get_message_str(self):
        return self.message

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def request_llm(self, **kwargs):
        kwargs["func_tool"] = kwargs.pop("tool_set", None)
        return Request(**kwargs)

    def plain_result(self, text):
        pytest.fail(text)


@pytest.fixture
def plugin(monkeypatch):
    logger = logging.getLogger(__name__)

    def module(name, **attrs):
        obj = types.ModuleType(name)
        obj.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, obj)
        return obj

    for name, path in ((PACKAGE, ROOT), (PACKAGE + ".utils", ROOT / "utils")):
        module(name, __path__=[str(path)])
    module("astrbot")
    module("astrbot.api", logger=logger)
    module("astrbot.api.all", logger=logger, AstrMessageEvent=Event, Context=object)
    module("astrbot.api.event", AstrMessageEvent=Event)
    module("astrbot.core")
    module("astrbot.core.provider")
    module("astrbot.core.provider.entities", ProviderRequest=Request)
    module("astrbot.core.star")
    module("astrbot.core.star.context", Context=object)
    module("astrbot.core.star.star", star_map={})
    module("astrbot.core.skills")
    module(
        "astrbot.core.skills.skill_manager",
        SkillManager=lambda: types.SimpleNamespace(list_skills=lambda **kwargs: []),
        build_skills_prompt=lambda skills: "",
    )

    def load(name, relative):
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        obj = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, obj)
        spec.loader.exec_module(obj)
        return obj

    reply = load(PACKAGE + ".utils.reply_handler", "utils/reply_handler.py")
    rewriter = load(
        PACKAGE + ".utils.system_prompt_rewriter", "utils/system_prompt_rewriter.py"
    )
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    hook = next(
        method
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for method in node.body
        if isinstance(method, ast.AsyncFunctionDef) and method.name == "on_llm_request"
    )
    hook.decorator_list = []
    namespace = {
        "__package__": PACKAGE,
        "SystemPromptRewriter": rewriter.SystemPromptRewriter,
        "logger": logger,
        "AstrMessageEvent": Event,
        "ProviderRequest": Request,
    }
    compiled = compile(
        ast.Module(body=[hook], type_ignores=[]), str(ROOT / "main.py"), "exec"
    )
    exec(compiled, namespace)  # noqa: S102 -- Only checked-in production code is executed.
    host = types.SimpleNamespace(
        debug_mode=False,
        _check_compliance_status=lambda: None,
        _snapshot_request_before_rewrite=lambda req: dict(req.__dict__),
        _extract_third_party_prompt_additions=lambda *args: [],
        _extract_third_party_context_additions=lambda *args: [],
    )

    async def prepare(event, request):
        await namespace["on_llm_request"](host, event, request)
        return request

    async def persona(_umo):
        return {"prompt": "固定人格设定", "_begin_dialogs_processed": []}

    context = types.SimpleNamespace(
        get_llm_tool_manager=lambda: types.SimpleNamespace(func_list=[]),
        persona_manager=types.SimpleNamespace(get_default_persona_v3=persona),
    )
    return types.SimpleNamespace(
        reply=reply, prepare=prepare, context=context, load=load
    )


@pytest.mark.parametrize("mode", ["append", "override"])
@pytest.mark.parametrize("include_sender", [True, False])
def test_changing_sender_reuses_complete_history_prefix(plugin, mode, include_sender):
    prompts = []
    for name, user_id in (("Alice", "101"), ("Bob", "202")):
        event = Event(name, user_id)
        request = asyncio.run(
            plugin.reply.ReplyHandler.generate_reply(
                event,
                plugin.context,
                HISTORY + "当前新消息",
                "自定义固定规则",
                mode,
                include_sender_info=include_sender,
                image_urls=["test-image"],
                audio_urls=["test-audio"],
            )
        )
        # Memory retrieval still sees just the current message before the final hook.
        assert request.prompt == event.message
        request = asyncio.run(plugin.prepare(event, request))
        assert request.system_prompt.startswith("固定人格设定")
        assert request.image_urls == ["test-image"]
        assert request.audio_urls == ["test-audio"]
        history_end = request.prompt.index(HISTORY) + len(HISTORY)
        if include_sender:
            target = f"[系统信息-当前对话对象] {name}（ID:{user_id}）"
            assert request.prompt.index(target) >= history_end
            assert (
                f"[对话对象确认] 你正在和 {name}（ID:{user_id}）对话" in request.prompt
            )
        else:
            assert "[系统信息-当前对话对象]" not in request.prompt
        prompts.append(request.prompt)
    prefix = os.path.commonprefix(prompts)
    assert HISTORY in prefix


@pytest.mark.parametrize("mood", [None, "开心", "疑惑"])
@pytest.mark.parametrize("proactive", [False, True])
def test_mood_keeps_system_and_history_stable_and_clears_event(plugin, mood, proactive):
    event = Event()
    base_prompt = HISTORY + "当前任务"
    reply = plugin.reply
    event.set_extra(reply.PLUGIN_REQUEST_MARKER, True)
    event.set_extra(reply.PLUGIN_CUSTOM_SYSTEM_PROMPT, "固定人格设定")
    event.set_extra(reply.PLUGIN_CUSTOM_STATIC_INSTRUCTIONS, "固定回复规则")
    event.set_extra(reply.PLUGIN_CUSTOM_PROMPT, base_prompt)
    hint = f"[系统信息-情绪参考: {mood}]" if mood else None
    event.set_extra("_group_chat_plus_mood_hint", hint)
    request = Request(
        prompt="" if proactive else event.message, system_prompt="固定人格设定"
    )
    # Proactive text_chat does not forward extra parts; regular requests can have memory.
    memory_part = object()
    if proactive:
        assert not hasattr(request, "extra_user_content_parts")
    else:
        request.extra_user_content_parts = [memory_part]
    request = asyncio.run(plugin.prepare(event, request))
    if not proactive:
        assert request.extra_user_content_parts == [memory_part]
    assert request.system_prompt == "固定人格设定\n\n固定回复规则"
    assert request.prompt.startswith(base_prompt)
    if hint:
        assert request.prompt.endswith(hint)
        assert request.prompt.count(hint) == 1
        assert hint not in request.system_prompt
    else:
        assert request.prompt == base_prompt
    assert event.get_extra("_group_chat_plus_mood_hint") is None
    # Saved original/full prompt extras are not changed to contain the runtime mood.
    assert event.message == "这件事怎么处理？"
    assert event.get_extra(reply.PLUGIN_CUSTOM_PROMPT) is None
    original_prompt = request.prompt
    asyncio.run(plugin.prepare(event, request))
    assert request.prompt == original_prompt


def test_non_plugin_request_is_not_rewritten(plugin):
    event = Event()
    event.set_extra("_group_chat_plus_mood_hint", "stale mood")
    request = Request(prompt="原始消息", system_prompt="原始系统提示")
    asyncio.run(plugin.prepare(event, request))
    assert request.prompt == "原始消息"
    assert request.system_prompt == "原始系统提示"


@pytest.mark.parametrize(
    "relative",
    [
        "utils/mood_tracker.py",
        "private_chat/private_chat_utils/private_chat_mood_tracker.py",
    ],
)
@pytest.mark.parametrize("mood", ["平静", "开心", "疑惑"])
def test_legacy_mood_injection_preserves_history_prefix(plugin, relative, mood):
    module = plugin.load(PACKAGE + ".mood_under_test", relative)
    tracker = object.__new__(module.MoodTracker)
    tracker.get_current_mood = lambda chat: mood
    result = tracker.inject_mood_to_prompt("test", HISTORY)
    assert result.startswith(HISTORY)
    if mood == tracker.DEFAULT_MOOD:
        assert result == HISTORY
    else:
        assert mood in result[len(HISTORY) :]
