"""Isolated attachment tests: local HTTP only, never a live OpenProject instance."""

import base64
import asyncio
import socket
import hashlib
import json
import logging
import os
from pathlib import Path
import unittest
import time

import httpx
import uvicorn
from unittest.mock import patch

from aiohttp import web
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import ValidationError
from starlette.exceptions import HTTPException

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
    from src import attachment_transfer
    from src.auth import APIKeyAuthProvider


def setUpModule():
    """Keep expected error cases from producing large framework trace logs."""
    global previous_logging_threshold
    previous_logging_threshold = logging.root.manager.disable
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(previous_logging_threshold)


class AttachmentTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp.http_app()), base_url="http://mcp.test"
        )
        self.addAsyncCleanup(self.http.aclose)
        self.addCleanup(self.clear_transfers)
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

    def clear_transfers(self):
        for reference in list(attachment_transfer.store.entries):
            attachment_transfer.store.discard(reference)

    async def stage(self, payload, name="handoff.md"):
        response = await self.http.post(
            "/attachments/uploads", params={"file_name": name}, content=payload,
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["file_ref"]

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
                    "file_ref": await self.stage(payload),
                }})
                sent = upload.data
                self.assertEqual(sent["uploaded_sha256"], hashlib.sha256(payload).hexdigest())
                downloaded = await client.call_tool("download_attachment", {"attachment_id": 42})
                self.assertNotIn("content_base64", downloaded.data)
                response = await self.http.get(downloaded.data["download_path"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, payload)
                self.assertIn("attachment;", response.headers["content-disposition"])
                self.assertEqual(downloaded.data["sha256"], sent["uploaded_sha256"])
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

    async def test_mcp_staged_upload_returns_matching_utf8_hash(self):
        with patch.object(attachments, "get_client", return_value=self.client):
            async with Client(mcp) as client:
                text = "# 文档\r\nHandoff\n"
                result = await client.call_tool("upload_work_package_attachment", {
                    "input": {
                        "work_package_id": 17,
                        "file_name": "handoff.md",
                        "file_ref": await self.stage(text.encode("utf-8")),
                    }
                })
                self.assertEqual(self.uploads[0]["bytes"], text.encode("utf-8"))
                self.assertEqual(
                    result.data["uploaded_sha256"],
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )

    async def test_http_upload_limits_empty_and_invalid_metadata(self):
        for params, content, headers, expected in (
            ({"file_name": "../bad"}, b"a", {}, 400),
            ({"file_name": "ok"}, b"", {}, 400),
            ({"file_name": "ok"}, b"a", {"Content-Type": "invalid"}, 400),
            ({}, b"a", {}, 400),
        ):
            response = await self.http.post("/attachments/uploads", params=params,
                                            content=content, headers=headers)
            self.assertEqual(response.status_code, expected)
        with patch.object(attachment_transfer, "MAX_ATTACHMENT_BYTES", 2):
            async def chunks():
                yield b"12"
                yield b"3"
            response = await self.http.post("/attachments/uploads?file_name=ok", content=chunks())
            self.assertEqual(response.status_code, 413)
        self.assertEqual(attachment_transfer.store.entries, {})
        self.assertEqual(list(Path(attachment_transfer.store.directory.name).iterdir()), [])

    async def test_expired_wrong_purpose_and_unknown_refs_rejected(self):
        reference = await self.stage(self.payload)
        response = await self.http.get(f"/attachments/downloads/{reference}")
        self.assertEqual(response.status_code, 404)
        transfer = attachment_transfer.store.entries[reference]
        transfer.expires_at = time.time() - 1
        # Windows may keep an expired download open; it must still be inaccessible.
        with patch.object(Path, "unlink", side_effect=PermissionError):
            with self.assertRaisesRegex(HTTPException, "expired or inaccessible"):
                attachment_transfer.store.get(reference, None, "upload")
        with patch.object(attachments, "get_client", return_value=self.client):
            async with Client(mcp) as client:
                for ref in (reference, "unknown", "../../secret"):
                    with self.assertRaises(ToolError):
                        await client.call_tool("upload_work_package_attachment", {"input": {
                            "work_package_id": 17, "file_ref": ref,
                        }})
        self.assertFalse(transfer.path.exists())
        self.assertEqual(self.requests, [])

    async def test_capacity_is_bounded(self):
        await self.stage(self.payload)
        with patch.object(attachment_transfer, "MAX_TRANSFERS", 1):
            response = await self.http.post("/attachments/uploads?file_name=ok", content=b"a")
            self.assertEqual(response.status_code, 503)

    async def test_authentication_and_owner_isolation(self):
        auth = APIKeyAuthProvider({"alice-key": "alice", "bob-key": "bob"})
        with patch.object(mcp, "auth", auth):
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                response = await self.http.post("/attachments/uploads?file_name=ok", content=b"a", headers=headers)
                self.assertEqual(response.status_code, 401)
            response = await self.http.post("/attachments/uploads?file_name=ok", content=self.payload,
                                            headers={"Authorization": "Bearer alice-key"})
            self.assertEqual(response.status_code, 201)
            reference = response.json()["file_ref"]
            with patch.object(attachments, "get_client", return_value=self.client):
                async with Client(mcp) as client:
                    with patch.object(attachments, "tool_owner", return_value=hashlib.sha256(b"bob-key").hexdigest()):
                        with self.assertRaises(ToolError):
                            await client.call_tool("upload_work_package_attachment", {"input": {
                                "work_package_id": 17, "file_ref": reference,
                            }})
                    with patch.object(attachments, "tool_owner", return_value=hashlib.sha256(b"alice-key").hexdigest()):
                        await client.call_tool("upload_work_package_attachment", {"input": {
                            "work_package_id": 17, "file_ref": reference,
                        }})
                        downloaded = await client.call_tool("download_attachment", {"attachment_id": 42})
            # Possessing the capability link is sufficient, regardless of login.
            for token in (None, "bob-key", "alice-key", "invalid-key"):
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                response = await self.http.get(downloaded.data["download_path"], headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, self.payload)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["referrer-policy"], "no-referrer")
            path = downloaded.data["download_path"]
            altered = path[:-1] + ("a" if path[-1] != "a" else "b")
            self.assertEqual((await self.http.get(altered)).status_code, 404)
            self.assertEqual((await self.http.get("/attachments/downloads/unknown")).status_code, 404)
            attachment_transfer.store.entries[downloaded.data["file_ref"]].expires_at = time.time() - 1
            self.assertEqual((await self.http.get(path)).status_code, 404)

    async def test_real_authenticated_http_mcp_transfer(self):
        with patch.object(mcp, "auth", APIKeyAuthProvider({"alice-key": "alice"})), \
             patch.object(attachments, "get_client", return_value=self.client):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(
                mcp.http_app(), log_level="critical", lifespan="on",
            ))
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async def wait_started():
                    while not server.started:
                        if task.done():
                            await task
                            raise RuntimeError("HTTP server stopped before starting")
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_started(), 5)
                base_url = f"http://127.0.0.1:{port}"
                async with httpx.AsyncClient(base_url=base_url, headers={
                    "Authorization": "Bearer alice-key",
                }) as http:
                    staged = await http.post("/attachments/uploads?file_name=blob.bin", content=self.payload)
                    self.assertEqual(staged.status_code, 201, staged.text)
                    async with Client(base_url + "/mcp", auth="alice-key") as client:
                        uploaded = await client.call_tool("upload_work_package_attachment", {"input": {
                            "work_package_id": 17, "file_ref": staged.json()["file_ref"],
                        }})
                        downloaded = await client.call_tool("download_attachment", {"attachment_id": 42})
                    # A separate browser has no MCP token or session cookies.
                    self.assertEqual(downloaded.data["download_url"], base_url + downloaded.data["download_path"])
                    async with httpx.AsyncClient() as browser:
                        unauthorized = await browser.post(base_url + "/mcp", json={
                            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "download_attachment", "arguments": {"attachment_id": 42}},
                        })
                        self.assertEqual(unauthorized.status_code, 401)
                        response = await browser.get(downloaded.data["download_url"])
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.content, self.payload)
                    self.assertEqual(downloaded.data["sha256"], uploaded.data["uploaded_sha256"])
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 5)
                listener.close()

    async def test_public_download_url_keeps_proxy_prefix(self):
        with patch.dict(os.environ, {"MCP_PUBLIC_URL": "https://mcp.example.com/service/"}), \
             patch.object(attachments, "get_client", return_value=self.client):
            async with Client(mcp) as client:
                downloaded = await client.call_tool("download_attachment", {"attachment_id": 42})
        self.assertEqual(downloaded.data["download_url"],
                         "https://mcp.example.com/service" + downloaded.data["download_path"])


class DownloadLinkTests(unittest.TestCase):
    def test_invalid_public_base_url_rejected(self):
        for url in ("file:///tmp", "https://user:secret@example.com", "https://example.com/?token=secret",
                    "https://example.com/#fragment", "example.com"):
            with self.subTest(url=url), patch.dict(os.environ, {"MCP_PUBLIC_URL": url}):
                with self.assertRaises(ValueError):
                    attachment_transfer.download_url("/attachments/downloads/ref")

    def test_no_http_context_keeps_path_only(self):
        with patch.dict(os.environ, {"MCP_PUBLIC_URL": ""}), \
             patch.object(attachment_transfer, "get_http_request", side_effect=RuntimeError):
            self.assertIsNone(attachment_transfer.download_url("/attachments/downloads/ref"))

    def test_access_log_omits_download_credentials(self):
        log_filter = attachment_transfer.DownloadAccessLogFilter()
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                                   '%s - "%s %s HTTP/1.1" %s',
                                   ("127.0.0.1", "GET", "/service/attachments/downloads/secret", 200), None)
        self.assertFalse(log_filter.filter(record))
        record.args = ("127.0.0.1", "POST", "/mcp", 200)
        self.assertTrue(log_filter.filter(record))


class AttachmentInputTests(unittest.TestCase):
    def model(self, **payload):
        return attachments.UploadWorkPackageAttachmentInput(
            work_package_id=17, **payload
        )

    def test_ref_required_and_inline_payloads_rejected(self):
        self.assertEqual(self.model(file_ref="opaque-ref").file_ref, "opaque-ref")
        for payload in ({}, {"file_ref": ""}, {"content": "a"},
                        {"file_ref": "ref", "content_base64": "YQ=="},
                        {"file_ref": "ref", "content": "a"}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                self.model(**payload)

    def test_basename_and_mime_control_characters_are_rejected(self):
        for name in ("../doc.md", "C:\\doc.md", ".", "..", " ", "bad\r\n.md"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.model(file_ref="ref", file_name=name)
        for mime in ("invalid", "text/plain\r\nX: bad"):
            with self.assertRaises(ValidationError):
                self.model(file_ref="ref", content_type=mime)


if __name__ == "__main__":
    unittest.main()
