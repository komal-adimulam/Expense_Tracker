"""
AgentRunner: the single entry point the bot uses.

One conversation thread per Telegram user. If the graph is paused on a human-in-the-loop
question, the next message is treated as the answer; if it is NOT a valid answer the pending
question is cancelled and the message is processed as a fresh request.
"""
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.core.types import is_cancel, parse_account

log = logging.getLogger(__name__)


@dataclass
class AgentReply:
    text: str
    choices: list[str] = field(default_factory=list)  # non-empty => ask the user to pick one
    txn_ids: list[int] = field(default_factory=list)  # transactions just recorded (for the Undo button)


def is_rate_limit(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "ratelimit" in text or "resource_exhausted" in text or "429" in text or "quota" in text


def message_text(msg) -> str:
    content = msg.content
    if isinstance(content, list):  # Gemini/Anthropic may return content blocks
        content = "".join(p if isinstance(p, str) else p.get("text", "") for p in content)
    return (content or "").strip()


class AgentRunner:
    def __init__(self, graph):
        self.graph = graph
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # single-process guard

    async def chat(self, *, user_id: int, thread_id: str, text: str, from_button: bool = False) -> AgentReply:
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}, "recursion_limit": 40}
        async with self._locks[thread_id]:
            pending = await self._pending_interrupt(config)

            if pending is not None:
                if is_cancel(text) or parse_account(text) is not None:
                    answer = "cancel" if is_cancel(text) else parse_account(text).value
                    return await self._run(Command(resume=answer), config)
                # unrelated message: drop the stale question, then handle the message normally
                await self._run(Command(resume="cancel"), config)
                if from_button:
                    return AgentReply("That question is no longer active.")
            elif from_button:
                return AgentReply("That question is no longer active.")

            return await self._run({"messages": [HumanMessage(text)]}, config)

    async def _run(self, graph_input, config) -> AgentReply:
        result = await self.graph.ainvoke(graph_input, config)
        pending = await self._pending_interrupt(config)
        if pending is not None:
            return AgentReply(pending["question"], pending.get("options", []) + ["cancel"])
        last_ai = next((m for m in reversed(result["messages"]) if isinstance(m, AIMessage)), None)
        text = (message_text(last_ai) if last_ai else "") or "Done."
        return AgentReply(text, txn_ids=(last_ai.additional_kwargs.get("txn_ids", []) if last_ai else []))

    async def _pending_interrupt(self, config) -> dict | None:
        state = await self.graph.aget_state(config)
        for task in state.tasks:
            for itr in task.interrupts:
                return itr.value
        return None
