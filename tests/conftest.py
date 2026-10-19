import asyncio
import itertools
import sys

import pytest

if sys.platform == "win32":  # psycopg async cannot run on the default Proactor loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent.graph import build_graph
from app.agent.runner import AgentRunner
from app.config import settings
from app.core import ledger
from app.db.session import init_db, session_scope

_ids = itertools.count(9_000_000 + int(__import__("time").time()) % 100_000 * 100)


class ScriptedLLM(BaseChatModel):
    """Plays back pre-written AI messages so the graph can be tested without a real model."""

    script: list = []
    idx: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self.script[self.idx]
        self.idx += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def tool_call(name: str, args: dict, id_: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id_, "type": "tool_call"}])


@pytest.fixture(scope="session")
async def saver():
    await init_db()
    pool = AsyncConnectionPool(
        settings.checkpointer_url, max_size=5, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    s = AsyncPostgresSaver(pool)
    await s.setup()
    yield s
    await pool.close()


@pytest.fixture
async def user_id():
    async with session_scope() as s:
        return (await ledger.get_or_create_user(s, next(_ids), "tester")).id


@pytest.fixture
def make_runner(saver):
    def _make(*script: AIMessage) -> AgentRunner:
        return AgentRunner(build_graph(ScriptedLLM(script=list(script)), saver))
    return _make
