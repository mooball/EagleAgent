import asyncio
from langchain_core.messages import HumanMessage
import chainlit as cl

async def sim():
    from includes.agents.browser_agent import BrowserAgent
    from langchain_google_genai import ChatGoogleGenerativeAI
    model = ChatGoogleGenerativeAI(model="gemini-1.5-flash")
    agent = BrowserAgent(model=model, store=None)
    
    config = {"configurable": {"thread_id": "789"}}
    inputs = {
        "messages": [HumanMessage(content="Use the browser agent to take a screenshot of https://example.com/ and return just the word Done.")],
        "user_id": "test_user"
    }

    # Simulate how app.py calls it
    async for event in agent.graph.astream_events(inputs, config=config, version="v1"):
        if event["event"] == "on_tool_end":
            print("SUB-AGENT TOOL END EVENT:", event.get("name"))
            data = event.get("data", {})
            output = data.get("output")
            output_str = str(output.content) if hasattr(output, "content") elseimport asyncio
from langchain_core.mest from langchaiouimport chainlit as cl

async def sim():
    froOT
async def sim():
   el    from includut    from langchain_google_genai import ChatGoogleGeneratitr    model = ChatGoogleGenerativeAI(model="gemini-1.5-flashved     agent = BrowserAgent(model=model, store=None)
    
    HO    
    config = {"configurable": {"thread_ == "_   in    inputs = {
     (sim())
