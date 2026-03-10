import asyncio
from langchain_core.messages import HumanMessage
import chainlit as cl
from includes.agents.browser_agent import build_browser_agent

async def sim():
    from config import config
    from includes.tools.browser_tools import browser
    agent = build_browser_agent(config.DEFAULT_MODEL)
    
    inputs = {
        "messages": [HumanMessage(content="take a screenshot of example.com")]
    }
    async for event in agent.astream_events(inputs, version="v1"):
        kind = event["event"]
        if kind == "on_tool_end":
            output = event.get("data", {}).get("output")
            print("TOOL END:", event["name"])
            print("OUTPUT TYPE:", type(output))
            if hasattr(output, "content"):
                print("CONTENT:", output.content[:100] if isinstance(output.content, str) else output.content)
            else:
                print("OUTPUT:", str(output)[:100])

if __name__ == "__main__":
    asyncio.run(sim())
