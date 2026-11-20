"""Exercise comment editing through FastMCP without sending live requests."""

import os
import unittest
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, call, patch

from fastmcp import Client
from fastmcp.exceptions import ToolError

from src.client import OpenProjectAPIError, OpenProjectClient

with patch.dict(os.environ, {
    "OPENPROJECT_URL": "http://127.0.0.1:1",
    "OPENPROJECT_API_KEY": "isolated-test-key",
    "OPENPROJECT_PROXY": "",
    "LOG_LEVEL": "ERROR",
}), patch("dotenv.load_dotenv", return_value=False):
    from src.server import mcp


class WorkPackageCommentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        upstream = OpenProjectClient("http://127.0.0.1:1", "isolated-test-key")
        self.request = AsyncMock()
        stack = AsyncExitStack()
        self.addAsyncCleanup(stack.aclose)
        stack.enter_context(patch.object(upstream, "_request", self.request))
        stack.enter_context(
            patch("src.tools.work_packages.get_client", return_value=upstream)
        )
        self.client = await stack.enter_async_context(Client(mcp))

    async def test_edits_the_existing_activity_without_changing_visibility(self):
        comment = "## Tuktuk\n\nUpdated rollout details."
        self.request.side_effect = [
            {"id": 123, "internal": True, "comment": {"raw": "Original"}},
            {"id": 123, "internal": True, "comment": {"raw": comment}},
        ]

        result = await self.client.call_tool(
            "update_work_package_comment", {"activity_id": 123, "comment": comment}
        )

        self.assertIn("Comment updated on activity #123", result.content[0].text)
        self.assertEqual(
            self.request.await_args_list,
            [
                call("GET", "/activities/123"),
                call("PATCH", "/activities/123", {"comment": comment}),
            ],
        )

    async def test_explicit_empty_comment_retracts_text(self):
        self.request.side_effect = [
            {"id": 123, "comment": {"raw": "Original"}},
            {"id": 123, "comment": {"raw": ""}},
        ]

        result = await self.client.call_tool(
            "update_work_package_comment", {"activity_id": 123, "comment": ""}
        )

        self.assertIn("Comment retracted on activity #123", result.content[0].text)
        self.request.assert_awaited_with(
            "PATCH", "/activities/123", {"comment": ""}
        )

    async def test_uses_the_object_form_after_comment_format_rejection(self):
        comment = "Updated text"
        self.request.side_effect = [
            {"id": 123, "comment": {"raw": "Original"}},
            OpenProjectAPIError(
                400,
                '{"message": "Bad request: comment is invalid"}',
                "API Error 400",
            ),
            {"id": 123, "comment": {"raw": comment}},
        ]

        result = await self.client.call_tool(
            "update_work_package_comment", {"activity_id": 123, "comment": comment}
        )

        self.assertIn("Comment updated on activity #123", result.content[0].text)
        self.assertEqual(
            self.request.await_args_list,
            [
                call("GET", "/activities/123"),
                call("PATCH", "/activities/123", {"comment": comment}),
                call("PATCH", "/activities/123", {"comment": {"raw": comment}}),
            ],
        )

    async def test_rejects_an_activity_without_a_comment(self):
        self.request.return_value = {"id": 123, "_type": "Activity", "comment": None}

        with self.assertRaisesRegex(ToolError, "no comment to edit"):
            await self.client.call_tool(
                "update_work_package_comment", {"activity_id": 123, "comment": "New text"}
            )

        self.request.assert_awaited_once_with("GET", "/activities/123")

    async def test_reports_permission_denial_as_an_mcp_error(self):
        self.request.side_effect = [
            {"id": 123, "comment": {"raw": "Original"}},
            OpenProjectAPIError(403, "", "API Error 403: Access denied"),
        ]

        with self.assertRaisesRegex(ToolError, "API Error 403"):
            await self.client.call_tool(
                "update_work_package_comment", {"activity_id": 123, "comment": "New text"}
            )

        self.assertEqual(self.request.await_count, 2)

    async def test_rejects_invalid_id_and_whitespace_only_text(self):
        for arguments in (
            {"activity_id": 0, "comment": "New text"},
            {"activity_id": 123, "comment": "  \n"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                await self.client.call_tool("update_work_package_comment", arguments)
        self.request.assert_not_called()

    async def test_does_not_report_success_without_an_activity_response(self):
        self.request.side_effect = [
            {"id": 123, "comment": {"raw": "Original"}},
            {},
        ]

        with self.assertRaisesRegex(ToolError, "did not confirm"):
            await self.client.call_tool(
                "update_work_package_comment", {"activity_id": 123, "comment": "New text"}
            )
