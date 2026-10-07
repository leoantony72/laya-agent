import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from winbrow.ultrafast import InteractiveElement, LayaUltrafastEngine, UltrafastStep


class TestLayaUltrafastEngine(unittest.TestCase):
    def setUp(self):
        self.mock_browser = MagicMock()
        self.mock_browser.execute_js = AsyncMock()
        self.mock_browser.type_in_element = AsyncMock()
        self.mock_browser.click_element = AsyncMock()
        self.mock_browser.navigate_to = AsyncMock()
        self.mock_browser.scroll_page = AsyncMock()
        self.mock_browser.read_page_text = AsyncMock()
        self.mock_browser.ensure_browser_open = AsyncMock()

        self.engine = LayaUltrafastEngine(self.mock_browser)

    def test_harvest_elements_parses_dom_nodes(self):
        self.mock_browser.execute_js.return_value = {
            "success": True,
            "value": {
                "url": "https://example.com",
                "title": "Example Domain",
                "elements": [
                    {
                        "index": 1,
                        "selector": "#search",
                        "tag_name": "input",
                        "element_type": "text",
                        "placeholder": "Search items...",
                        "value": "",
                        "is_visible": True,
                    },
                    {
                        "index": 2,
                        "selector": "button.submit",
                        "tag_name": "button",
                        "text": "Submit Search",
                        "is_visible": True,
                    },
                ],
            },
        }

        harvest = asyncio.run(self.engine.harvest_elements())
        self.assertEqual(harvest["url"], "https://example.com")
        self.assertEqual(harvest["title"], "Example Domain")
        self.assertEqual(len(harvest["elements"]), 2)
        self.assertEqual(harvest["elements"][0].placeholder, "Search items...")
        self.assertEqual(harvest["elements"][1].text, "Submit Search")

    def test_make_typed_decision_for_search_goal(self):
        elements = [
            InteractiveElement(
                index=1,
                selector="#search-box",
                tag_name="input",
                element_type="text",
                placeholder="Search...",
            )
        ]
        decision = self.engine.make_typed_decision(
            goal="search for RTX 4090 on amazon",
            url="https://amazon.com",
            title="Amazon.com",
            elements=elements,
            previous_steps=[],
        )
        self.assertEqual(decision.action, "type")
        self.assertEqual(decision.target_index, 1)
        self.assertEqual(decision.value, "RTX 4090")

    def test_make_typed_decision_for_click_goal(self):
        elements = [
            InteractiveElement(
                index=1,
                selector="#login-btn",
                tag_name="button",
                text="Sign In",
            )
        ]
        decision = self.engine.make_typed_decision(
            goal="click sign in button",
            url="https://example.com",
            title="Example",
            elements=elements,
            previous_steps=[],
        )
        self.assertEqual(decision.action, "click")
        self.assertEqual(decision.target_index, 1)

    def test_run_task_end_to_end_mocked(self):
        self.mock_browser.execute_js.return_value = {
            "success": True,
            "value": {
                "url": "https://example.com",
                "title": "Example Domain",
                "elements": [
                    {
                        "index": 1,
                        "selector": "input#q",
                        "tag_name": "input",
                        "element_type": "text",
                        "placeholder": "Search...",
                    }
                ],
            },
        }
        self.mock_browser.type_in_element.return_value = {"success": True}
        self.mock_browser.read_page_text.return_value = {
            "success": True,
            "text": "Found RTX 4090 graphics card starting at $1599",
        }

        res = asyncio.run(self.engine.run_task("search for RTX 4090", max_steps=2))
        self.assertTrue(res.success)
        self.assertEqual(res.goal, "search for RTX 4090")
        self.assertGreater(len(res.steps), 0)


if __name__ == "__main__":
    unittest.main()
