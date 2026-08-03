from pathlib import Path
import unittest


class RoutingIntegrationSourceTests(unittest.TestCase):
    def test_shared_fallback_primitive_enforces_policy_and_candidate_projection(self):
        source = Path("src/llm_core.py").read_text(encoding="utf-8")
        start = source.index("async def stream_llm_with_fallback(")
        body = source[start:]
        self.assertIn("filter_fallback_candidates", body)
        self.assertIn("project_messages_for_candidate", body)
        self.assertIn("candidate_messages", body)
        self.assertIn("stream_llm(url, model, candidate_messages", body)
        self.assertNotIn("stream_llm(url, model, messages, headers=headers", body)

    def test_cross_provider_opt_in_is_documented(self):
        env = Path(".env.example").read_text(encoding="utf-8")
        self.assertIn("ODYSSEUS_AGENT_FALLBACK_POLICY=same_endpoint", env)
        self.assertIn("ODYSSEUS_AGENT_ALLOW_CROSS_PROVIDER_FALLBACK=0", env)


if __name__ == "__main__":
    unittest.main()
