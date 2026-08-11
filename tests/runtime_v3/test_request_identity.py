import json
import tempfile
from pathlib import Path
import unittest

from src.agent.contracts import AgentRunRequest
from src.agent.runtime_v3.ledger import DurableRunLedger
from src.agent.runtime_v3.request_identity import (
    redacted_endpoint,
    safe_request_snapshot,
    semantic_request_digest,
)


class RequestIdentityTests(unittest.TestCase):
    @staticmethod
    def _request(content: str, *, api_key: str = "secret-one") -> AgentRunRequest:
        return AgentRunRequest.from_legacy_arguments(
            endpoint_url="https://user:password@example.com/v1/chat/completions?api_key=query-secret#frag",
            model="model-a",
            messages=[{"role": "user", "content": content}],
            headers={"Authorization": f"Bearer {api_key}", "X-Tenant": "tenant-a"},
            session_id="session-1",
            owner="owner-1",
            disabled_tools={"dangerous"},
            relevant_tools={"read_files", "patch_workspace"},
            forced_tools={"read_files"},
            workspace="/private/workspace/path",
            uploaded_files=[{"name": "private.txt", "content": "upload secret"}],
            approved_plan="private plan text",
        )

    def test_semantic_digest_changes_when_same_count_prompt_changes(self):
        first = self._request("first prompt")
        second = self._request("different prompt")
        self.assertEqual(len(first.messages), len(second.messages))
        self.assertNotEqual(
            semantic_request_digest(first),
            semantic_request_digest(second),
        )

    def test_header_values_affect_identity_without_becoming_diagnostics(self):
        first = self._request("same prompt", api_key="secret-one")
        second = self._request("same prompt", api_key="secret-two")
        self.assertNotEqual(
            semantic_request_digest(first),
            semantic_request_digest(second),
        )
        encoded = json.dumps(safe_request_snapshot(first), sort_keys=True)
        self.assertNotIn("secret-one", encoded)
        self.assertNotIn("Bearer", encoded)
        self.assertIn("Authorization", encoded)

    def test_snapshot_contains_no_prompt_upload_plan_or_workspace_plaintext(self):
        request = self._request("top secret prompt")
        snapshot = safe_request_snapshot(request)
        encoded = json.dumps(snapshot, sort_keys=True)
        for secret in (
            "top secret prompt",
            "upload secret",
            "private plan text",
            "/private/workspace/path",
            "password",
            "query-secret",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(snapshot["message_count"], 1)
        self.assertEqual(snapshot["uploaded_file_count"], 1)
        self.assertTrue(snapshot["semantic_request_sha256"])
        self.assertTrue(snapshot["workspace_sha256"])

    def test_endpoint_drops_userinfo_query_and_fragment(self):
        self.assertEqual(
            redacted_endpoint(
                "https://user:password@example.com:8443/v1/chat?token=secret#frag"
            ),
            "https://example.com:8443/v1/chat",
        )

    def test_ledger_rejects_same_run_id_for_semantically_different_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = DurableRunLedger(Path(tmp) / "ledger.sqlite3")
            try:
                first = self._request("first prompt")
                second = self._request("different prompt")
                ledger.create_run(
                    run_id="fixed-run",
                    session_id="session-1",
                    owner="owner-1",
                    workload="foreground",
                    request=safe_request_snapshot(first),
                    limits={},
                    model="model-a",
                    endpoint=redacted_endpoint(first.endpoint_url),
                )
                with self.assertRaisesRegex(RuntimeError, "run_id collision"):
                    ledger.create_run(
                        run_id="fixed-run",
                        session_id="session-1",
                        owner="owner-1",
                        workload="foreground",
                        request=safe_request_snapshot(second),
                        limits={},
                        model="model-a",
                        endpoint=redacted_endpoint(second.endpoint_url),
                    )
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
