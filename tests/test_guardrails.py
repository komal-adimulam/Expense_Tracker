from langchain_core.messages import AIMessage

from app.agent.guardrails import GREETING, SCOPE_MESSAGE
from tests.conftest import tool_call


async def test_off_topic_tool_returns_fixed_refusal(user_id, make_runner):
    runner = make_runner(tool_call("respond_to_user", {"kind": "off_topic", "message": "Sure! def fib(n): ..."}))
    reply = await runner.chat(user_id=user_id, thread_id=f"g{user_id}", text="write fibonacci in python")
    assert reply.text == SCOPE_MESSAGE  # the model's own wording is ignored for off_topic


async def test_model_free_text_is_never_forwarded(user_id, make_runner):
    # a model that ignores the rules and answers directly -> replaced by the refusal
    runner = make_runner(AIMessage("The capital of France is Paris."))
    reply = await runner.chat(user_id=user_id, thread_id=f"g{user_id}", text="capital of france?")
    assert reply.text == SCOPE_MESSAGE and "Paris" not in reply.text


async def test_greeting_and_clarify(user_id, make_runner):
    runner = make_runner(
        tool_call("respond_to_user", {"kind": "greeting"}),
        tool_call("respond_to_user", {"kind": "clarify", "message": "How much did you spend?"}, "c2"),
    )
    assert (await runner.chat(user_id=user_id, thread_id=f"g{user_id}", text="hi")).text == GREETING
    reply = await runner.chat(user_id=user_id, thread_id=f"g{user_id}", text="I bought lunch")
    assert reply.text == "❓ How much did you spend?"


async def test_clarify_cannot_smuggle_long_answers(user_id, make_runner):
    essay = "Here is a long essay about python. " * 30
    runner = make_runner(tool_call("respond_to_user", {"kind": "clarify", "message": essay}))
    reply = await runner.chat(user_id=user_id, thread_id=f"g{user_id}", text="tell me about python")
    assert len(reply.text) <= 205
