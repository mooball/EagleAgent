"""LangGraph construction, model factory, and shared async globals.

This module owns the PostgreSQL connection pool, LangGraph checkpointer,
cross-thread store, MCP client, and the compiled agent graphs.  Call
``await setup_globals()`` once (idempotent) before using any of them.
"""

import asyncio
import logging
import os
from typing import TypedDict, Sequence, Annotated, Dict, Any, Literal, NotRequired

from langchain_core.messages import BaseMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool

from config import config
from includes.mcp_config import load_mcp_config
from includes.agents import (
    BrowserAgent, GeneralAgent, ProcurementAgent, Supervisor, SysAdminAgent,
)
from includes.job_runner import JobRunner

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool access control
# ---------------------------------------------------------------------------
ADMIN_ONLY_TOOLS = ["delete_all_user_data"]

# ---------------------------------------------------------------------------
# PostgreSQL connection pool (opened lazily in setup_globals)
# ---------------------------------------------------------------------------
pg_pool = AsyncConnectionPool(
    config.CHECKPOINT_DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={
        "autocommit": True,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    },
    open=False,
)

# ---------------------------------------------------------------------------
# Shared async globals — initialised by setup_globals()
# ---------------------------------------------------------------------------
store = None               # AsyncPostgresStore (cross-thread memory)
checkpointer = None        # AsyncPostgresSaver (thread state)
mcp_client = None           # MultiServerMCPClient | None
graph = None               # Compiled Eagle Agent multi-agent graph
sysadmin_graph = None      # Compiled System Admin single-agent graph
research_graph = None      # Compiled Research Agent single-agent graph
job_runner = JobRunner()    # Background script runner

globals_initialized = False


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------
class SupervisorState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    user_id: str  # User email for cross-thread memory lookup
    file_attachments: NotRequired[list[Dict[str, Any]]]
    next_agent: NotRequired[str]
    intent_context: NotRequired[str]


# ---------------------------------------------------------------------------
# Model factory
# ---------------------------------------------------------------------------
def create_model(agent_name: str) -> ChatGoogleGenerativeAI:
    """Create a model instance for a specific agent, using per-agent model overrides."""
    return ChatGoogleGenerativeAI(
        model=config.get_agent_model(agent_name),
        temperature=config.DEFAULT_TEMPERATURE,
        max_output_tokens=config.DEFAULT_MAX_TOKENS,
    )


# ---------------------------------------------------------------------------
# One-time async initialisation
# ---------------------------------------------------------------------------
async def setup_globals():
    """Initialize async-dependent global variables (idempotent)."""
    global store, mcp_client, checkpointer, graph, sysadmin_graph, research_graph, globals_initialized

    if globals_initialized:
        return

    # Open pg_pool
    try:
        await pg_pool.open()
    except Exception:
        pass

    # Start the background job runner
    await job_runner.start()

    # Set up store
    store = AsyncPostgresStore(pg_pool)
    await store.setup()

    # Set up checkpointer
    checkpointer = AsyncPostgresSaver(pg_pool)
    await checkpointer.setup()

    # Set up MCP
    try:
        mcp_config = load_mcp_config("config/mcp_servers.yaml")
        if mcp_config:
            from langchain_mcp_adapters.client import MultiServerMCPClient
            mcp_client = MultiServerMCPClient(mcp_config)
            logger.info(f"MCP client initialized with {len(mcp_config)} server(s)")
        else:
            logger.info("No MCP servers configured")
    except Exception as e:
        logger.warning(f"Failed to initialize MCP client: {e}. Agent will work without MCP tools.")
        mcp_client = None

    # Initialize agents
    browser_agent = BrowserAgent(model=create_model("BrowserAgent"), store=store)
    procurement_agent = ProcurementAgent(model=create_model("ProcurementAgent"), store=store)
    general_agent = GeneralAgent(
        model=create_model("GeneralAgent"), store=store,
        mcp_client=mcp_client, admin_only_tools=ADMIN_ONLY_TOOLS,
    )
    supervisor_node = Supervisor(model=create_model("Supervisor"))

    from includes.agents import ResearchAgent
    research_agent = ResearchAgent(
        model=create_model("ResearchAgent"), store=store,
        include_rfq_tools=True,
    )

    # ---- Eagle Agent multi-agent graph ----
    async def run_supervisor(state, config):
        return await supervisor_node(state, config)

    async def run_general(state, config):
        return await general_agent(state, config)

    async def run_procurement(state, config):
        return await procurement_agent(state, config)

    async def run_research(state, config):
        return await research_agent(state, config)

    builder = StateGraph(SupervisorState)
    builder.add_node("Supervisor", run_supervisor)
    builder.add_node("GeneralAgent", run_general)
    builder.add_node("ProcurementAgent", run_procurement)
    builder.add_node("ResearchAgent", run_research)
    builder.add_edge(START, "Supervisor")

    def router(state: SupervisorState) -> Literal["GeneralAgent", "ProcurementAgent", "ResearchAgent", "__end__"]:
        next_agent = state.get("next_agent", "FINISH")
        if next_agent == "GeneralAgent":
            return "GeneralAgent"
        elif next_agent == "ProcurementAgent":
            return "ProcurementAgent"
        elif next_agent == "ResearchAgent":
            return "ResearchAgent"
        else:
            return END

    builder.add_conditional_edges("Supervisor", router)
    builder.add_edge("GeneralAgent", "Supervisor")
    builder.add_edge("ProcurementAgent", "Supervisor")
    builder.add_edge("ResearchAgent", "Supervisor")
    graph = builder.compile(checkpointer=checkpointer, store=store)

    # ---- System Admin single-agent graph ----
    sysadmin_agent = SysAdminAgent(
        model=create_model("SysAdminAgent"), store=store, job_runner=job_runner,
    )

    async def run_sysadmin(state, config):
        return await sysadmin_agent(state, config)

    sa_builder = StateGraph(SupervisorState)
    sa_builder.add_node("SysAdminAgent", run_sysadmin)
    sa_builder.add_edge(START, "SysAdminAgent")
    sa_builder.add_edge("SysAdminAgent", END)
    sysadmin_graph = sa_builder.compile(checkpointer=checkpointer, store=store)

    # ---- Research single-agent graph (standalone profile, no RFQ tools) ----
    standalone_research_agent = ResearchAgent(
        model=create_model("ResearchAgent"), store=store,
        include_rfq_tools=False,
    )

    async def run_standalone_research(state, config):
        return await standalone_research_agent(state, config)

    ra_builder = StateGraph(SupervisorState)
    ra_builder.add_node("ResearchAgent", run_standalone_research)
    ra_builder.add_edge(START, "ResearchAgent")
    ra_builder.add_edge("ResearchAgent", END)
    research_graph = ra_builder.compile(checkpointer=checkpointer, store=store)

    globals_initialized = True
