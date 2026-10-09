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
        self.mock_browser.get_current_url = AsyncMock(return_value={"url": "about:blank", "title": ""})

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

    def test_make_typed_decision_for_flight_goal(self):
        elements = [
            InteractiveElement(
                index=1,
                selector="[data-wb-idx='1']",
                tag_name="input",
                role="combobox",
                placeholder="Where from?",
                label="Where from?",
            ),
            InteractiveElement(
                index=2,
                selector="[data-wb-idx='2']",
                tag_name="input",
                role="combobox",
                placeholder="Where to?",
                label="Where to?",
            ),
            InteractiveElement(
                index=3,
                selector="[data-wb-idx='3']",
                tag_name="button",
                text="Search flights",
            ),
        ]
        # Step 1: fills origin
        d1 = self.engine.make_typed_decision(
            goal="find flights from New York to London",
            url="https://google.com/travel/flights",
            title="Google Flights",
            elements=elements,
            previous_steps=[],
        )
        self.assertEqual(d1.action, "type")
        self.assertEqual(d1.target_index, 1)
        self.assertEqual(d1.value, "New York")

        # Step 2: fills destination
        d2 = self.engine.make_typed_decision(
            goal="find flights from New York to London",
            url="https://google.com/travel/flights",
            title="Google Flights",
            elements=elements,
            previous_steps=[d1],
        )
        self.assertEqual(d2.action, "type")
        self.assertEqual(d2.target_index, 2)
        self.assertEqual(d2.value, "London")

        # Step 3: clicks search button
        d3 = self.engine.make_typed_decision(
            goal="find flights from New York to London",
            url="https://google.com/travel/flights",
            title="Google Flights",
            elements=elements,
            previous_steps=[d1, d2],
        )
        self.assertEqual(d3.action, "click")
        self.assertEqual(d3.target_index, 3)

    def test_decide_with_laya_mocked(self):
        fake_worker = MagicMock()
        fake_worker.apredict = AsyncMock(return_value={
            "answers": {
                "action": {"choice": "click", "confidence": 0.95},
                "target": {"choice": "el_2", "confidence": 0.9},
            }
        })
        engine = LayaUltrafastEngine(self.mock_browser, worker=fake_worker)
        elements = [
            InteractiveElement(index=1, selector="b1", tag_name="button", text="Cancel"),
            InteractiveElement(index=2, selector="b2", tag_name="button", text="Select Flight"),
        ]
        step = asyncio.run(engine.decide_with_laya(
            goal="select flight",
            url="https://flights.com",
            title="Results",
            elements=elements,
            previous_steps=[],
        ))
        self.assertIsNotNone(step)
        self.assertEqual(step.action, "click")
        self.assertEqual(step.target_index, 2)


if __name__ == "__main__":
    unittest.main()
