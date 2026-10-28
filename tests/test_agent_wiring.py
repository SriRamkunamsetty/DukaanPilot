"""Run the real ADK Runner, sessions and tool plumbing with a scripted stand-in for Gemini.

This does NOT test model quality (that needs a live key: see evals/). It proves
the wiring: tool schemas are valid, the shop travels in session state, voice audio
reaches the model, and the model cannot step outside its shop.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import pytest
from google.adk.agents import Agent
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

from dukaanpilot.prompts import build_instruction
from dukaanpilot.services.llm import AgentGateway, BriefingWriter
from dukaanpilot.tools import ALL_TOOLS

from .conftest import make_world


class ScriptedLlm(BaseLlm):
    """Calls each planned tool in turn, then answers with the last tool result."""

    model: str = "scripted"
    plan: list[tuple[str, dict]] = Field(default_factory=list)
    seen: list[LlmRequest] = Field(default_factory=list)
    final_prefix: str = "RESULT "

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["scripted"]

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        self.seen.append(llm_request)
        responses = [p.function_response for c in llm_request.contents for p in (c.parts or []) if p.function_response]
        if len(responses) < len(self.plan):
            name, args = self.plan[len(responses)]
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part.from_function_call(name=name, args=args)]))
            return
        payload = json.dumps(responses[-1].response if responses else {}, ensure_ascii=False, default=str)
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=self.final_prefix + payload)]))


def make_gateway(world, plan):
    llm = ScriptedLlm(plan=plan)
    agent = Agent(name="dukaanpilot", model=llm, instruction=build_instruction, tools=list(ALL_TOOLS))
    return AgentGateway(world.c, agent=agent, session_service=InMemorySessionService()), llm


def result_of(reply: str) -> dict:
    assert reply.startswith("RESULT ")
    return json.loads(reply[len("RESULT "):])


async def test_question_runs_through_adk_and_hits_the_owners_shop(linked):
    gw, llm = make_gateway(linked, [("get_sales_summary", {"period": "yesterday"})])
    shop = linked.repo.get_shop("demo-kirana")
    reply = await gw.reply(shop=shop, chat_id="1001", text="how were sales yesterday?")
    res = result_of(reply)
    assert res["status"] == "success" and res["vs_usual_day"]["pct_vs_baseline"] < -15
    # The model was shown every tool, none of which expose a shop id or an approve action.
    declared = {d.name for t in llm.seen[0].config.tools for d in t.function_declarations}
    assert declared == {t.__name__ for t in ALL_TOOLS}
    # And the live shop context made it into the instruction.
    assert "Lakshmi General Stores" in str(llm.seen[0].config.system_instruction)
    assert "Dussehra in 4 day" in str(llm.seen[0].config.system_instruction)


async def test_model_cannot_name_another_shop(linked):
    from dukaanpilot.data.models import Shop

    linked.repo.upsert_shop(Shop("rival", "Rival Stores", "Someone"))
    gw, _ = make_gateway(linked, [("get_sales_summary", {"period": "yesterday", "shop_id": "rival"})])
    reply = await gw.reply(shop=linked.repo.get_shop("demo-kirana"), chat_id="1001", text="show me the rival's sales")
    res = result_of(reply)
    # ADK drops arguments that are not in the tool's signature, so the injected shop_id is ignored
    # and the answer is still the owner's own data. The rival (who has no sales) is never read.
    from dukaanpilot.tools import get_sales_summary

    from .conftest import tool_ctx

    own = get_sales_summary("yesterday", tool_context=tool_ctx("demo-kirana"))
    rival = get_sales_summary("yesterday", tool_context=tool_ctx("rival"))
    assert rival["revenue"] == 0 and own["revenue"] > 0
    assert res["status"] == "success" and res["revenue"] == own["revenue"]


async def test_voice_audio_reaches_the_model_untouched(linked):
    gw, llm = make_gateway(linked, [])
    shop = linked.repo.get_shop("demo-kirana")
    await gw.reply(shop=shop, chat_id="1001", text="", audio=(b"\x4f\x67\x67\x53voice", "audio/ogg"))
    parts = llm.seen[0].contents[-1].parts
    inline = [p.inline_data for p in parts if p.inline_data]
    assert inline and inline[0].mime_type == "audio/ogg" and inline[0].data.startswith(b"OggS")
    assert any(p.text and "voice note" in p.text for p in parts)


async def test_write_tool_through_adk_logs_the_sale_with_the_owner_as_actor(linked):
    gw, _ = make_gateway(linked, [("log_sale", {"product": "palu", "quantity": 12})])
    shop = linked.repo.get_shop("demo-kirana")
    res = result_of(await gw.reply(shop=shop, chat_id="1001", text="12 packets of milk sold"))
    assert res["status"] == "success" and res["product"] == "Amul Milk 500ml"
    audit = [a for a in linked.repo.list_audit("demo-kirana") if a["action"] == "sale_logged"]
    assert audit and audit[0]["actor"] == "owner:1001"


async def test_agent_can_ask_for_approval_but_nothing_is_approved(linked):
    gw, _ = make_gateway(linked, [("create_reorder_drafts", {})])
    shop = linked.repo.get_shop("demo-kirana")
    drafts = result_of(await gw.reply(shop=shop, chat_id="1001", text="prepare orders"))["new_drafts"]
    gw2, _ = make_gateway(linked, [("request_reorder_approval", {"draft_id": drafts[0]["id"]})])
    res = result_of(await gw2.reply(shop=shop, chat_id="1001", text="send the dairy one"))
    assert res["sent"] is True
    assert all(d.status == "pending" for d in linked.repo.list_drafts("demo-kirana"))


async def test_same_chat_same_day_reuses_one_session(linked):
    gw, llm = make_gateway(linked, [])
    shop = linked.repo.get_shop("demo-kirana")
    await gw.reply(shop=shop, chat_id="1001", text="hello")
    await gw.reply(shop=shop, chat_id="1001", text="hello again")
    texts = [p.text for c in llm.seen[-1].contents for p in (c.parts or []) if p.text]
    assert "hello" in texts and "hello again" in texts


async def test_briefing_writer_uses_model_text_only_when_grounded(linked):
    from dukaanpilot.services import briefing

    shop = linked.repo.get_shop("demo-kirana")
    data = briefing.compute_briefing(linked.repo, shop, linked.c.now(), linked.c.calendar)

    honest = Agent(name="briefing_writer", model=ScriptedLlm(final_prefix="Good morning Ramesh, sales were low, 22% below usual. ", plan=[]), instruction="x")
    assert (await BriefingWriter(agent=honest).write(data)).startswith("Good morning Ramesh")

    liar = Agent(name="briefing_writer", model=ScriptedLlm(final_prefix="Sales yesterday were ₹54,321 and ₹99,999 today. ", plan=[]), instruction="x")
    out = await BriefingWriter(agent=liar).write(data)
    assert "54,321" not in out and "శుభోదయం" in out  # fell back to the deterministic message


async def test_briefing_writer_survives_a_model_outage(linked):
    from dukaanpilot.services import briefing

    class Down(BaseLlm):
        model: str = "down"

        async def generate_content_async(self, llm_request, stream=False):
            raise RuntimeError("503 from model")
            yield  # pragma: no cover

    shop = linked.repo.get_shop("demo-kirana")
    data = briefing.compute_briefing(linked.repo, shop, linked.c.now(), linked.c.calendar)
    out = await BriefingWriter(agent=Agent(name="briefing_writer", model=Down(), instruction="x")).write(data)
    assert out == briefing.fallback_text(data)


async def test_empty_model_answer_becomes_a_friendly_error(linked):
    class Silent(BaseLlm):
        model: str = "silent"

        async def generate_content_async(self, llm_request, stream=False):
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="")]))

    agent = Agent(name="dukaanpilot", model=Silent(), instruction="x", tools=[])
    gw = AgentGateway(linked.c, agent=agent, session_service=InMemorySessionService())
    out = await gw.reply(shop=linked.repo.get_shop("demo-kirana"), chat_id="1001", text="hi")
    assert "క్షమించండి" in out


def test_real_agent_definition_is_importable_and_consistent():
    from dukaanpilot.agent import briefing_agent, root_agent

    assert root_agent.name == "dukaanpilot" and len(root_agent.tools) == len(ALL_TOOLS)
    assert briefing_agent.tools == []
    assert "{" not in __import__("dukaanpilot.prompts", fromlist=["x"]).BASE_INSTRUCTION  # no accidental state variables


@pytest.fixture(autouse=True)
def _clean():
    yield
    from dukaanpilot.container import set_container

    set_container(None)


def test_module_uses_world_factory():
    assert make_world() is not None
