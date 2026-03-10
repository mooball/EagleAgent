import asyncio
from langchain_core.messages import HumanMessage
from app import mcp_client, store
import chainlit as cl

cl.user_session.set("thread_id", "123")

async def sim():
    from app import graph
    inputs = {
        "messages": [HumanMessage(content="Use the browser agent to take a screenshot of https://example.com/ and return just the word Done.")],
        "user_id": "test_user"
    }
    async for event in graph.astream_events(inputs, config={"configurable": {"thread_id": "123"}}, version="v1"):
        kind = event["event"]
        if kind == "on_tool_end":
            output = event.get("data", {}).get("output")
            print("TOOL END EVENT! Name:", event.get("name"))
            print("OUTPUT TYPE:", type(output))
            if hasattr(output, "content"):
                print("CONTENT:", str(output.content)[:100])
            else:
                print("OUTPUT:", str(output)[:100])

if __name__ == "__main__":
    asyncio.run(sim())
