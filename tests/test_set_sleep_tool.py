"""set_sleep 工具测试"""
import sys
import os
import unittest
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ai_tools
from bot import consciousness as cs


class TestSetSleepTool(unittest.TestCase):

    def test_tool_definition_exists(self):
        names = [t["name"] for t in ai_tools.ALL_TOOLS]
        self.assertIn("set_sleep", names)

    def test_tool_not_in_dangerous(self):
        self.assertNotIn("set_sleep", ai_tools.DANGEROUS_TOOLS)

    def test_execute_triggers_sleep(self):
        state = cs.get_state()
        state["sleeping"] = False
        state["sleeping_until"] = ""

        async def run():
            class FakeBot:
                pass
            result = await ai_tools.execute_tool(
                FakeBot(), 0, 0, "set_sleep", {"minutes": 90}
            )
            return result

        result = asyncio.run(run())
        self.assertTrue(state["sleeping"])
        self.assertTrue(len(state["sleeping_until"]) > 0)
        self.assertIn("睡", result)


if __name__ == "__main__":
    unittest.main()
