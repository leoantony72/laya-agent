import asyncio
import json
from winbrow.browser_task import BrowserController

async def main():
    b = BrowserController()
    # Click the first result header
    res = await b.click_element("#rso a h3")
    print("click result:", res)
    await asyncio.sleep(3.5)
    
    # Read page text
    text_res = await b.read_page_text()
    txt = text_res.get("text", "")
    print("navigated page text len:", len(txt))
    print("sample:", txt[:500])

if __name__ == "__main__":
    asyncio.run(main())
