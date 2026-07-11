"""Focused tests for user lookup permission handling."""

import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from src.client import OpenProjectAPIError, OpenProjectClient


os.environ.setdefault("OPENPROJECT_URL", "https://example.test")
os.environ.setdefault("OPENPROJECT_API_KEY", "api-key")


class UserLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_current_user_uses_me_endpoint(self):
        client = OpenProjectClient("https://example.test", "api-key")
        captured = {}

        async def fake_request(method, endpoint, data=None):
            captured["method"] = method
            captured["endpoint"] = endpoint
            return {"id": 20, "name": "Pingdong Yi"}

        client._request = fake_request

        result = await client.get_current_user()

        self.assertEqual(captured, {"method": "GET", "endpoint": "/users/me"})
        self.assertEqual(result["id"], 20)

    async def test_get_current_user_tool_formats_user(self):
        from src.tools import users as users_module

        class FakeClient:
            async def get_current_user(self):
                return {
                    "id": 20,
                    "name": "Pingdong Yi",
                    "email": "pyi@example.com",
                    "login": "pyi@example.com",
                    "status": "active",
                    "admin": False,
                    "language": "en",
                }

        with patch.object(users_module, "get_client", return_value=FakeClient()):
            result = await users_module.get_current_user.fn()

        self.assertIn("Current User #20", result)
        self.assertIn("Pingdong Yi", result)
        self.assertIn("**Admin**: No", result)
        self.assertIn("**Language**: en", result)

    async def test_get_principals_encodes_filters(self):
        client = OpenProjectClient("https://example.test", "api-key")
        captured = {}

        async def fake_request(method, endpoint, data=None):
            captured["method"] = method
            captured["endpoint"] = endpoint
            return {}

        client._request = fake_request
        filters = json.dumps(
            [{"type": {"operator": "=", "values": ["User"]}}]
        )

        result = await client.get_principals(filters)

        self.assertEqual(captured["method"], "GET")
        self.assertEqual(
            unquote(captured["endpoint"]), f"/principals?filters={filters}"
        )
        self.assertEqual(result["_embedded"]["elements"], [])

    async def test_list_users_falls_back_to_visible_principals(self):
        from src.tools import users as users_module

        class FakeClient:
            async def get_users(self, filters):
                body = json.dumps(
                    {
                        "errorIdentifier": (
                            "urn:openproject-org:api:v3:errors:MissingPermission"
                        ),
                        "message": "You are not authorized to access this resource.",
                    }
                )
                raise OpenProjectAPIError(403, body, "API Error 403")

            async def get_principals(self, filters):
                parsed_filters = json.loads(filters)
                expected = [
                    {"type": {"operator": "=", "values": ["User"]}},
                    {
                        "any_name_attribute": {
                            "operator": "~",
                            "values": ["Pingdong"],
                        }
                    },
                    {"status": {"operator": "=", "values": ["1"]}},
                ]
                self_test.assertEqual(parsed_filters, expected)
                return {
                    "_embedded": {
                        "elements": [
                            {
                                "id": 7,
                                "name": "Pingdong Example",
                                "status": "active",
                            }
                        ]
                    }
                }

        self_test = self
        with patch.object(users_module, "get_client", return_value=FakeClient()):
            result = await users_module.list_users.fn(
                name="Pingdong", status="active"
            )

        self.assertIn("Found 1 visible user", result)
        self.assertIn("Pingdong Example", result)
        self.assertIn("Limited result", result)

    async def test_list_users_does_not_hide_other_forbidden_errors(self):
        from src.tools import users as users_module

        class FakeClient:
            async def get_users(self, filters):
                raise OpenProjectAPIError(
                    403, "{}", "API Error 403: forbidden"
                )

        with patch.object(users_module, "get_client", return_value=FakeClient()):
            result = await users_module.list_users.fn()

        self.assertIn("Failed to list users", result)
        self.assertIn("API Error 403: forbidden", result)


if __name__ == "__main__":
    unittest.main()
