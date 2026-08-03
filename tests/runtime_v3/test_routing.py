import os
import unittest
from unittest.mock import patch

from src.agent.runtime_v3.context import ContextBudgetExceeded
from src.agent.runtime_v3.routing import (
    FallbackPolicy,
    filter_fallback_candidates,
    load_fallback_policy,
    project_messages_for_candidate,
)


class RoutingPolicyTests(unittest.TestCase):
    def test_same_endpoint_model_fallback_is_allowed(self):
        plan = filter_fallback_candidates(
            [
                ("https://api.openai.com/v1/chat/completions", "gpt-primary", {"Authorization": "secret"}),
                ("https://api.openai.com/v1/chat/completions", "gpt-fallback", {"Authorization": "other"}),
            ],
            policy=FallbackPolicy.SAME_ENDPOINT,
        )
        self.assertEqual([candidate[1] for candidate in plan.accepted], ["gpt-primary", "gpt-fallback"])
        self.assertEqual(plan.rejected, ())

    def test_cross_provider_is_blocked_by_default_without_secret_leak(self):
        plan = filter_fallback_candidates(
            [
                ("https://api.openai.com/v1/chat/completions?token=hidden", "gpt", {"Authorization": "Bearer TOPSECRET"}),
                ("https://api.anthropic.com/v1/messages?api_key=hidden", "claude", {"x-api-key": "TOPSECRET"}),
            ],
            policy=FallbackPolicy.SAME_ENDPOINT,
        )
        self.assertEqual(len(plan.accepted), 1)
        self.assertEqual(plan.rejected[0].reason, "different_endpoint")
        diagnostic = str(plan.rejected[0].trust_domain)
        self.assertNotIn("TOPSECRET", diagnostic)
        self.assertNotIn("api_key", diagnostic)
        self.assertNotIn("/v1/messages", diagnostic)

    def test_local_to_remote_is_blocked_in_same_trust_domain_mode(self):
        plan = filter_fallback_candidates(
            [
                ("http://127.0.0.1:11434/api/chat", "local", None),
                ("https://api.openai.com/v1/chat/completions", "remote", None),
            ],
            policy=FallbackPolicy.SAME_TRUST_DOMAIN,
        )
        self.assertEqual(len(plan.accepted), 1)
        self.assertEqual(plan.rejected[0].reason, "locality_boundary")

    def test_cross_provider_requires_conspicuous_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_fallback_policy(), FallbackPolicy.SAME_ENDPOINT)
        with patch.dict(
            os.environ,
            {"ODYSSEUS_AGENT_FALLBACK_POLICY": "explicit_cross_provider"},
            clear=True,
        ):
            self.assertEqual(load_fallback_policy(), FallbackPolicy.SAME_ENDPOINT)
        with patch.dict(
            os.environ,
            {"ODYSSEUS_AGENT_ALLOW_CROSS_PROVIDER_FALLBACK": "1"},
            clear=True,
        ):
            self.assertEqual(load_fallback_policy(), FallbackPolicy.EXPLICIT_CROSS_PROVIDER)

    def test_explicit_cross_provider_policy_allows_candidate(self):
        plan = filter_fallback_candidates(
            [
                ("https://api.openai.com/v1/chat/completions", "gpt", None),
                ("https://api.anthropic.com/v1/messages", "claude", None),
            ],
            policy=FallbackPolicy.EXPLICIT_CROSS_PROVIDER,
        )
        self.assertEqual(len(plan.accepted), 2)


class CandidateProjectionTests(unittest.TestCase):
    @staticmethod
    def estimate(messages):
        return sum(len(str(message.get("content") or "")) + 10 for message in messages)

    def test_each_candidate_uses_its_own_context_window(self):
        calls = []

        def context_window(endpoint, model):
            calls.append((endpoint, model))
            return 4096 if model == "small" else 16384

        messages = [
            {"role": "system", "content": "system invariant"},
            {"role": "user", "content": "old" * 1000},
            {"role": "user", "content": "latest request"},
        ]
        small = project_messages_for_candidate(
            messages,
            endpoint_url="https://same.example/v1",
            model="small",
            max_output_tokens=512,
            context_length_fn=context_window,
            estimate_tokens_fn=self.estimate,
        )
        large = project_messages_for_candidate(
            messages,
            endpoint_url="https://same.example/v1",
            model="large",
            max_output_tokens=512,
            context_length_fn=context_window,
            estimate_tokens_fn=self.estimate,
        )
        self.assertEqual(calls, [
            ("https://same.example/v1", "small"),
            ("https://same.example/v1", "large"),
        ])
        self.assertLess(small.input_budget, large.input_budget)
        self.assertGreaterEqual(small.dropped_messages, large.dropped_messages)
        self.assertEqual(small.messages[0]["role"], "system")
        self.assertEqual(small.messages[-1]["content"], "latest request")

    def test_tool_call_and_result_remain_indivisible(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
            {"role": "user", "content": "latest"},
        ]
        projected = project_messages_for_candidate(
            messages,
            endpoint_url="https://same.example/v1",
            model="m",
            max_output_tokens=512,
            context_length_fn=lambda *_: 4096,
            estimate_tokens_fn=self.estimate,
        )
        roles = [message["role"] for message in projected.messages]
        self.assertEqual(roles.count("assistant"), roles.count("tool"))

    def test_projection_fails_instead_of_truncating_critical_semantics(self):
        with self.assertRaises(ContextBudgetExceeded):
            project_messages_for_candidate(
                [
                    {"role": "system", "content": "x" * 2000},
                    {"role": "user", "content": "y" * 2000},
                ],
                endpoint_url="https://same.example/v1",
                model="tiny",
                max_output_tokens=512,
                context_length_fn=lambda *_: 2048,
                estimate_tokens_fn=self.estimate,
            )


if __name__ == "__main__":
    unittest.main()
