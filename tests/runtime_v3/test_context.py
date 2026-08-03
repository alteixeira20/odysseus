import unittest

from src.agent.runtime_v3.context import ContextBudgetExceeded, project_context


def estimate(messages):
    return sum(len(str(m.get("content", ""))) + 10 for m in messages)


class ContextProjectionTests(unittest.TestCase):
    def test_preserves_system_latest_user_and_tool_pair(self):
        messages = [
            {"role": "system", "content": "critical"},
            {"role": "user", "content": "old" * 30},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
            {"role": "user", "content": "latest"},
        ]
        projected = project_context(messages, budget_tokens=100, estimate_tokens=estimate)
        self.assertEqual(projected.messages[0]["role"], "system")
        self.assertEqual(projected.messages[-1]["content"], "latest")
        roles = [m["role"] for m in projected.messages]
        self.assertEqual(roles.count("assistant"), roles.count("tool"))

    def test_refuses_to_truncate_critical_semantics(self):
        with self.assertRaises(ContextBudgetExceeded):
            project_context(
                [
                    {"role": "system", "content": "x" * 100},
                    {"role": "user", "content": "y" * 100},
                ],
                budget_tokens=50,
                estimate_tokens=estimate,
            )


if __name__ == "__main__":
    unittest.main()
