import asyncio
import os
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

load_dotenv()

async def test_stream():
    model = ChatGoogleGenerativeAI(model="gemini-3-flash-preview", google_api_key=os.getenv("GOOGLE_API_KEY"))
    messages = [HumanMessage(content="Hello, say something simple.")]
    
    print("Starting stream...")
    async for chunk in model.astream(messages):
        print(f"Chunk content type: {type(chunk.content)}")
        print(f"Chunk content: {chunk.content}")

if __name__ == "__main__":
    asyncio.run(test_stream())
