"""Isolated attachment tests: local HTTP only, never a live OpenProject instance."""

import base64
import hashlib
import json
import logging
import os
import unittest
from unittest.mock import patch

from aiohttp import web
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from src.client import OpenProjectAPIError, OpenProjectClient

# Import and register tools with dummy configuration, without loading a real .env.
with patch.dict(os.environ, {
    "OPENPROJECT_URL": "http://127.0.0.1:1/openproject",
    "OPENPROJECT_API_KEY": "isolated-test-key",
    "OPENPROJECT_PROXY": "",
    "LOG_LEVEL": "ERROR",
}), patch("dotenv.load_dotenv", return_value=False):
    from src.server import mcp
    from src.tools import attachments


def setUpModule():
    """Keep expected error cases from producing large framework trace logs."""
    global previous_logging_threshold
    previous_logging_threshold = logging.root.manager.disable
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(previous_logging_threshold)


class AttachmentTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.payload = b"\x00\xff\r\nAttachment bytes\n"
        self.requests = []
        self.uploads = []
        self.storage_headers = []
        self.deleted = False
        self.metadata = {
            "id": 42, "fileName": "handoff.md", "contentType": "text/markdown",
            "fileSize": len(self.payload),
            "_links": {"container": {"href": "/openproject/api/v3/work_packages/17"}},
        }
        storage = web.Application()
        storage.router.add_get("/blob", self.storage)
        self.storage_runner = web.AppRunner(storage)
        await self.storage_runner.setup()
        self.addAsyncCleanup(self.storage_runner.cleanup)
        storage_site = web.TCPSite(self.storage_runner, "127.0.0.1", 0)
        await storage_site.start()
        storage_port = storage_site._server.sockets[0].getsockname()[1]
        self.storage_url = f"http://127.0.0.1:{storage_port}/blob"

        app = web.Application(client_max_size=20 * 1024 * 1024)
        app.router.add_route("*", "/openproject/api/v3/{tail:.*}", self.api)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.addAsyncCleanup(self.runner.cleanup)
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.client = OpenProjectClient(
            f"http://127.0.0.1:{port}/openproject", "isolated-test-key"
        )

    async def storage(self, request):
        self.storage_headers.append(dict(request.headers))
        return web.Response(body=self.payload)

    async def api(self, request):
        self.requests.append((request.method, request.path, dict(request.headers)))
        tail = request.match_info["tail"]
        for status in (403, 404, 413):
            if f"/{status}" in tail:
                return web.json_response({
                    "_type": "Error", "errorIdentifier": "attachment-test-error",
                    "message": f"Attachment error {status}",
                }, status=status)
        if tail == "attachments/302/content":
            raise web.HTTPFound(self.storage_url)
        if tail == "work_packages/302/attachments" and request.method == "POST":
            raise web.HTTPTemporaryRedirect(self.storage_url)
        if tail == "attachments/43/content":
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(self.payload)
            await response.write_eof()
            return response
        if tail == "attachments/42/content":
            if self.deleted:
                return web.json_response({"message": "Attachment not found"}, status=404)
            return web.Response(body=self.payload, content_type="application/octet-stream")
        if tail == "work_packages":
            return web.json_response({"received": await request.json()})
        if tail == "attachments/42":
            if self.deleted:
                return web.json_response({"message": "Attachment not found"}, status=404)
            if request.method == "DELETE":
                self.deleted = True
                return web.Response(status=204)
            return web.json_response(self.metadata)
        if tail == "work_packages/17/attachments" and request.method == "POST":
            reader = await request.multipart()
            metadata = await reader.next()
            if (
                metadata.name != "metadata"
                or metadata.headers.get("Content-Type") != "application/json"
            ):
                return web.json_response(
                    {"message": "Expected application/json metadata part"}, status=415
                )
            metadata_text = await metadata.text()
            file = await reader.next()
            file_bytes = bytes(await file.read())
            self.uploads.append({
                "metadata_name": metadata.name,
                "metadata_type": metadata.headers.get("Content-Type"),
                "metadata_filename": metadata.filename,
                "metadata": json.loads(metadata_text),
                "file_name": file.name, "filename": file.filename,
                "file_type": file.headers.get("Content-Type"), "bytes": file_bytes,
                "extra_part": await reader.next(),
            })
            self.payload = file_bytes
            self.metadata["fileSize"] = len(file_bytes)
            self.metadata["fileName"] = self.uploads[-1]["metadata"]["fileName"]
            return web.json_response(self.metadata, status=201)
        if tail == "work_packages/17/attachments":
            elements = [] if self.deleted else [self.metadata]
            return web.json_response({
                "_type": "Collection", "total": len(elements), "count": len(elements),
                "_embedded": {"elements": elements},
            })
        return web.json_response({"message": "Fixture endpoint not found"}, status=404)

    async def test_upload_multipart_preserves_text_bytes_and_metadata(self):
        payload = "# 交接文档\r\n\nTreatment exports\n".encode("utf-8")
        result = await self.client.upload_work_package_attachment(
            17, "handoff.md", payload, description="Repository handoff"
        )
        self.assertEqual(result["id"], 42)
        upload = self.uploads[0]
        self.assertEqual(upload["metadata_name"], "metadata")
        self.assertIsNone(upload["metadata_filename"])
        self.assertEqual(upload["metadata_type"], "application/json")
        self.assertEqual(upload["metadata"], {
            "fileName": "handoff.md",
            "description": {"format": "plain", "raw": "Repository handoff"},
        })
        self.assertEqual(upload["file_name"], "file")
        self.assertEqual(upload["filename"], "handoff.md")
        self.assertEqual(upload["file_type"], "text/markdown")
        self.assertEqual(upload["bytes"], payload)
        self.assertIsNone(upload["extra_part"])
        headers = self.requests[0][2]
        self.assertTrue(headers["Content-Type"].startswith("multipart/form-data; boundary="))
        auth = base64.b64encode(b"apikey:isolated-test-key").decode("ascii")
        self.assertEqual(headers["Authorization"], f"Basic {auth}")

    async def test_binary_upload_and_default_content_type(self):
        payload = self.payload
        await self.client.upload_work_package_attachment(17, "blob.unknown", payload)
        self.assertEqual(self.uploads[0]["bytes"], payload)
        self.assertEqual(self.uploads[0]["file_type"], "application/octet-stream")
        self.assertNotIn("description", self.uploads[0]["metadata"])

    async def test_json_transport_still_works_after_upload(self):
        await self.client.upload_work_package_attachment(17, "handoff.md", b"hello")
        result = await self.client._request("POST", "/work_packages", {"subject": "same"})
        self.assertEqual(result["received"], {"subject": "same"})
        self.assertEqual(self.requests[-1][2]["Content-Type"], "application/json")
        self.assertEqual(self.client.headers["Content-Type"], "application/json")

    async def test_configured_proxy_applies_to_upload_and_download(self):
        self.client.proxy = self.client.base_url.removesuffix("/openproject")
        await self.client.upload_work_package_attachment(17, "handoff.md", b"proxy test")
        self.assertEqual(await self.client.download_attachment(42), b"proxy test")

    async def test_list_and_get_metadata_preserve_hal_container(self):
        result = await self.client.list_work_package_attachments(17)
        self.assertEqual(result["_embedded"]["elements"][0], self.metadata)
        self.assertEqual(await self.client.get_attachment(42), self.metadata)

    async def test_download_preserves_non_utf8_bytes(self):
        self.assertEqual(await self.client.download_attachment(42), self.payload)
        self.assertEqual(self.requests[-1][2]["Accept"], "application/octet-stream")

    async def test_delete_handles_empty_response_and_removes_attachment(self):
        self.assertTrue(await self.client.delete_attachment(42))
        method, path, headers = self.requests[-1]
        self.assertEqual((method, path), ("DELETE", "/openproject/api/v3/attachments/42"))
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        listed = await self.client.list_work_package_attachments(17)
        self.assertEqual(listed["_embedded"]["elements"], [])
        for action in (self.client.get_attachment, self.client.download_attachment,
                       self.client.delete_attachment):
            with self.subTest(action=action.__name__):
                with self.assertRaises(OpenProjectAPIError) as raised:
                    await action(42)
                self.assertEqual(raised.exception.status, 404)

    async def test_api_errors_keep_status_identifier_and_message(self):
        for status in (403, 404, 413):
            with self.subTest(status=status):
                for action in (
                    self.client.get_attachment(status),
                    self.client.download_attachment(status),
                    self.client.delete_attachment(status),
                    self.client.upload_work_package_attachment(status, "test.md", b"test"),
                ):
                    with self.assertRaises(OpenProjectAPIError) as raised:
                        await action
                    self.assertEqual(raised.exception.status, status)
                    self.assertEqual(raised.exception.api_message, f"Attachment error {status}")
                    self.assertEqual(raised.exception.error_identifier, "attachment-test-error")

    async def test_download_limit_checks_content_length(self):
        with patch("src.client.MAX_ATTACHMENT_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "download limit"):
                await self.client.download_attachment(42)

    async def test_download_limit_checks_chunked_response(self):
        with patch("src.client.MAX_ATTACHMENT_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "download limit"):
                await self.client.download_attachment(43)

    async def test_storage_redirect_does_not_forward_api_credentials(self):
        self.assertEqual(await self.client.download_attachment(302), self.payload)
        self.assertTrue(self.requests[0][2]["Authorization"].startswith("Basic "))
        self.assertNotIn("Authorization", self.storage_headers[0])

    async def test_upload_redirect_is_not_replayed(self):
        with self.assertRaisesRegex(OpenProjectAPIError, "upload redirected"):
            await self.client.upload_work_package_attachment(302, "test.md", b"test")
        self.assertEqual(self.storage_headers, [])

    async def test_invalid_client_upload_rejected_before_network(self):
        for name, payload in (("../bad.md", b"test"), ("ok.md", b"")):
            with self.assertRaises(ValueError):
                await self.client.upload_work_package_attachment(17, name, payload)
        with patch("src.client.MAX_ATTACHMENT_BYTES", 2):
            with self.assertRaises(ValueError):
                await self.client.upload_work_package_attachment(17, "ok.md", b"123")
        for method in (self.client.get_attachment, self.client.download_attachment,
                       self.client.list_work_package_attachments,
                       self.client.delete_attachment):
            for invalid_id in (0, -1):
                with self.assertRaises(ValueError):
                    await method(invalid_id)
        self.assertEqual(self.requests, [])

    async def test_mcp_registration_schema_and_full_upload_download(self):
        with patch.object(attachments, "get_client", return_value=self.client):
            async with Client(mcp) as client:
                registered = {tool.name: tool for tool in await client.list_tools()}
                names = {"list_work_package_attachments", "upload_work_package_attachment",
                         "get_attachment", "download_attachment", "delete_attachment"}
                self.assertTrue(names.issubset(registered))
                self.assertTrue(registered["get_attachment"].annotations.readOnlyHint)
                self.assertFalse(registered["upload_work_package_attachment"].annotations.idempotentHint)
                deletion = registered["delete_attachment"]
                self.assertFalse(deletion.annotations.readOnlyHint)
                self.assertTrue(deletion.annotations.destructiveHint)
                self.assertTrue(deletion.annotations.idempotentHint)
                self.assertEqual(deletion.inputSchema["required"], ["attachment_id"])
                self.assertEqual(deletion.inputSchema["properties"]["attachment_id"]["exclusiveMinimum"], 0)
                payload = "# Handoff\r\n交接\n".encode("utf-8")
                upload = await client.call_tool("upload_work_package_attachment", {"input": {
                    "work_package_id": 17, "file_name": "handoff.md",
                    "content_base64": base64.b64encode(payload).decode("ascii"),
                }})
                sent = upload.data
                self.assertEqual(sent["uploaded_sha256"], hashlib.sha256(payload).hexdigest())
                downloaded = await client.call_tool("download_attachment", {"attachment_id": 42})
                self.assertEqual(base64.b64decode(downloaded.data["content_base64"]), payload)
                self.assertEqual(downloaded.data["sha256"], sent["uploaded_sha256"])
                compact = await client.call_tool("download_attachment", {
                    "attachment_id": 42, "include_content": False,
                })
                self.assertNotIn("content_base64", compact.data)
                self.assertEqual(compact.data["size_bytes"], len(payload))
                listed = await client.call_tool("list_work_package_attachments", {"work_package_id": 17})
                self.assertEqual(listed.data["total"], 1)
                metadata = await client.call_tool("get_attachment", {"attachment_id": 42})
                self.assertEqual(metadata.data["_links"]["container"], self.metadata["_links"]["container"])
                deleted = await client.call_tool("delete_attachment", {"attachment_id": 42})
                self.assertEqual(deleted.data, {"attachment_id": 42, "deleted": True})
                listed = await client.call_tool("list_work_package_attachments", {"work_package_id": 17})
                self.assertEqual(listed.data["total"], 0)
                with self.assertRaisesRegex(ToolError, "Failed to delete attachment: API Error 404"):
                    await client.call_tool("delete_attachment", {"attachment_id": 42})

    async def test_mcp_delete_reports_api_errors_and_rejects_invalid_ids(self):
        with patch.object(attachments, "get_client", return_value=self.client):
            async with Client(mcp) as client:
                for status in (403, 404):
                    with self.subTest(status=status):
                        with self.assertRaisesRegex(ToolError, f"Failed to delete attachment: API Error {status}"):
                            await client.call_tool("delete_attachment", {"attachment_id": status})
                count = len(self.requests)
                for invalid_id in (0, -1):
                    with self.subTest(attachment_id=invalid_id), self.assertRaises(ToolError):
                        await client.call_tool("delete_attachment", {"attachment_id": invalid_id})
                self.assertEqual(len(self.requests), count)

    async def test_mcp_reports_api_failure_and_invalid_id_as_errors(self):
        with patch.object(attachments, "get_client", return_value=self.client):
            async with Client(mcp) as client:
                with self.assertRaisesRegex(ToolError, "Attachment error 403"):
                    await client.call_tool("get_attachment", {"attachment_id": 403})
                count = len(self.requests)
                with self.assertRaises(ToolError):
                    await client.call_tool("get_attachment", {"attachment_id": 0})
                self.assertEqual(len(self.requests), count)

    async def test_mcp_text_upload_returns_matching_utf8_hash(self):
        with patch.object(attachments, "get_client", return_value=self.client):
            async with Client(mcp) as client:
                text = "# 文档\r\nHandoff\n"
                result = await client.call_tool("upload_work_package_attachment", {
                    "input": {
                        "work_package_id": 17,
                        "file_name": "handoff.md",
                        "content": text,
                    }
                })
                self.assertEqual(self.uploads[0]["bytes"], text.encode("utf-8"))
                self.assertEqual(
                    result.data["uploaded_sha256"],
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )


class AttachmentInputTests(unittest.TestCase):
    def model(self, **payload):
        return attachments.UploadWorkPackageAttachmentInput(
            work_package_id=17, file_name="handoff.md", **payload
        )

    def test_text_encoded_as_utf8_and_binary_decoded_exactly(self):
        self.assertEqual(self.model(content="交接\r\n").decoded_content(), "交接\r\n".encode("utf-8"))
        self.assertEqual(self.model(content_base64="AP8=").decoded_content(), b"\x00\xff")

    def test_exactly_one_nonempty_payload_required(self):
        for payload in ({}, {"content": "a", "content_base64": "YQ=="},
                        {"content": ""}, {"content_base64": ""}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                self.model(**payload)

    def test_invalid_base64_is_rejected(self):
        for value in ("data:text/plain;base64,YQ==", "YQ==\n", "%%%", "YQ", "é"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.model(content_base64=value)

    def test_basename_and_mime_control_characters_are_rejected(self):
        for name in ("../doc.md", "C:\\doc.md", ".", "..", " ", "bad\r\n.md"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                attachments.UploadWorkPackageAttachmentInput(
                    work_package_id=17, file_name=name, content="a"
                )
        for mime in ("invalid", "text/plain\r\nX: bad"):
            with self.assertRaises(ValidationError):
                self.model(content="a", content_type=mime)

    def test_decoded_byte_limit_includes_multibyte_utf8(self):
        with patch("src.tools.attachments.MAX_ATTACHMENT_BYTES", 3):
            self.assertEqual(self.model(content="交").decoded_content(), "交".encode("utf-8"))
            with self.assertRaises(ValidationError):
                self.model(content="交接")
            with self.assertRaises(ValidationError):
                self.model(content_base64=base64.b64encode(b"1234").decode("ascii"))


if __name__ == "__main__":
    unittest.main()
