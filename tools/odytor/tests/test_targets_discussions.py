"""Discussion target tests with a mocked GitHub client. No live gh calls.

These cover the fetch control flow that only runs against GraphQL: comment
pagination, nested reply pagination, owner/name splitting, metadata cleanup,
and the not-found error paths.
"""

import unittest
from unittest import mock

from odytor.errors import FetchError
from odytor.targets.discussions import fetch_discussion


def comment(cid, body, replies=None, more_replies=False, reply_cursor=None):
    return {
        "id": cid,
        "body": body,
        "author": {"login": "commenter"},
        "replies": {
            "pageInfo": {"hasNextPage": more_replies, "endCursor": reply_cursor},
            "nodes": replies or [],
        },
    }


def discussion_payload(nodes, more_comments=False, end_cursor=None, **overrides):
    discussion = {
        "number": 7,
        "title": "A discussion",
        "body": "main post",
        "category": {"name": "Q&A", "slug": "q-a"},
        "author": {"login": "asker"},
        "comments": {
            "pageInfo": {"hasNextPage": more_comments, "endCursor": end_cursor},
            "nodes": nodes,
        },
    }
    discussion.update(overrides)
    return {"data": {"repository": {"discussion": discussion}}}


def reply_payload(nodes, more=False, end_cursor=None):
    return {
        "data": {
            "node": {
                "replies": {
                    "pageInfo": {"hasNextPage": more, "endCursor": end_cursor},
                    "nodes": nodes,
                }
            }
        }
    }


class FetchDiscussionTests(unittest.TestCase):
    def test_single_page_no_replies(self):
        client = mock.Mock(repo="owner/repo")
        client.graphql.return_value = discussion_payload([comment("c1", "hello")])

        data = fetch_discussion(client, 7)

        self.assertEqual(data.metadata["number"], 7)
        self.assertEqual(data.metadata["title"], "A discussion")
        self.assertEqual([c["body"] for c in data.comments], ["hello"])
        self.assertEqual(data.comments[0]["replies"], [])
        client.graphql.assert_called_once()

    def test_bulky_comments_connection_is_stripped_from_metadata(self):
        client = mock.Mock(repo="owner/repo")
        client.graphql.return_value = discussion_payload([comment("c1", "hi")])

        data = fetch_discussion(client, 7)

        self.assertNotIn("comments", data.metadata)

    def test_owner_and_name_are_split_from_repo(self):
        client = mock.Mock(repo="some-owner/some-repo")
        client.graphql.return_value = discussion_payload([])

        fetch_discussion(client, 7)

        _, variables = client.graphql.call_args.args
        self.assertEqual(variables["owner"], "some-owner")
        self.assertEqual(variables["repo"], "some-repo")
        self.assertEqual(variables["number"], 7)
        self.assertIsNone(variables["cursor"])

    def test_paginates_comments_and_forwards_cursor(self):
        client = mock.Mock(repo="owner/repo")
        client.graphql.side_effect = [
            discussion_payload([comment("c1", "first")], more_comments=True, end_cursor="CUR"),
            discussion_payload([comment("c2", "second")]),
        ]

        data = fetch_discussion(client, 7)

        self.assertEqual([c["body"] for c in data.comments], ["first", "second"])
        second_call_vars = client.graphql.call_args_list[1].args[1]
        self.assertEqual(second_call_vars["cursor"], "CUR")

    def test_paginates_replies_for_a_comment(self):
        client = mock.Mock(repo="owner/repo")
        first_reply = {"id": "r1", "body": "reply-1", "author": {"login": "x"}}
        second_reply = {"id": "r2", "body": "reply-2", "author": {"login": "y"}}
        client.graphql.side_effect = [
            discussion_payload(
                [comment("c1", "root", replies=[first_reply],
                         more_replies=True, reply_cursor="RCUR")]
            ),
            reply_payload([second_reply]),
        ]

        data = fetch_discussion(client, 7)

        self.assertEqual([r["body"] for r in data.comments[0]["replies"]], ["reply-1", "reply-2"])
        reply_call_vars = client.graphql.call_args_list[1].args[1]
        self.assertEqual(reply_call_vars["commentId"], "c1")
        self.assertEqual(reply_call_vars["cursor"], "RCUR")

    def test_missing_discussion_raises_fetch_error(self):
        client = mock.Mock(repo="owner/repo")
        client.graphql.return_value = {"data": {"repository": {"discussion": None}}}

        with self.assertRaises(FetchError) as cm:
            fetch_discussion(client, 404)

        self.assertIn("Could not find discussion #404 in owner/repo", str(cm.exception))

    def test_missing_repository_raises_fetch_error(self):
        client = mock.Mock(repo="owner/repo")
        client.graphql.return_value = {"data": {"repository": None}}

        with self.assertRaises(FetchError):
            fetch_discussion(client, 7)


if __name__ == "__main__":
    unittest.main()
