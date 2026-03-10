import asyncio
from includes.tools.browser_tools import take_screenshot, browser

async def main():
    print("Testing take_screenshot...")
    try:
        res = await take_screenshot.ainvoke({})
        print("Result:", res)
    except Exception as e:
        print("Error:", e)

asyncio.run(main())
