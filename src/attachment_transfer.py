"""Temporary binary HTTP transfers, kept outside MCP messages."""

import hashlib
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from fastmcp.server.dependencies import get_access_token, get_http_request
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

from src.client import MAX_ATTACHMENT_BYTES, validate_attachment_filename
from src.attachment_errors import attachment_error

TRANSFER_TTL_SECONDS = 900
MAX_TRANSFERS = 100


def attachment_http_errors(operation):
    """Return JSON errors while preserving HTTP status and authentication headers."""
    def decorate(handler):
        @wraps(handler)
        async def wrapped(request):
            try:
                return await handler(request)
            except Exception as error:
                details = attachment_error(error, operation)
                status = error.status_code if isinstance(error, HTTPException) else (
                    400 if isinstance(error, ValueError) else 500
                )
                details["http_status"] = status
                headers = {"Cache-Control": "no-store"}
                if isinstance(error, HTTPException) and error.headers:
                    headers.update(error.headers)
                if status >= 500 and not isinstance(error, HTTPException):
                    logging.getLogger(__name__).exception("Attachment %s failed", operation)
                return JSONResponse({"error": details}, status_code=status, headers=headers)
        return wrapped
    return decorate


class DownloadAccessLogFilter(logging.Filter):
    """Keep capability URLs out of the server's HTTP access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/attachments/downloads/" not in record.getMessage()


def download_url(path: str) -> str | None:
    """Use an explicit public base URL for proxies, otherwise the HTTP request."""
    base_url = os.getenv("MCP_PUBLIC_URL", "").strip().rstrip("/")
    if base_url:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("MCP_PUBLIC_URL must be an HTTP(S) base URL without credentials, query or fragment")
    else:
        try:
            base_url = str(get_http_request().base_url).rstrip("/")
        except RuntimeError:
            return None
    return base_url + path


@dataclass
class Transfer:
    path: Path
    file_name: str
    content_type: str
    owner: str | None
    purpose: str
    expires_at: float
    size_bytes: int = 0
    sha256: str = ""


class TransferStore:
    """Bounded, process-local temporary files with opaque, expiring references."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="openproject-transfers-")
        self.entries: dict[str, Transfer] = {}

    def prune(self) -> None:
        for reference, transfer in list(self.entries.items()):
            if transfer.expires_at <= time.time():
                try:
                    transfer.path.unlink(missing_ok=True)
                except PermissionError:
                    # Windows cannot unlink a transfer that is still being served.
                    # Reject expired references now and retry cleanup next time.
                    continue
                del self.entries[reference]

    def create(
        self, file_name: str, content_type: str, owner: str | None, purpose: str,
    ) -> tuple[str, Transfer]:
        self.prune()
        if len(self.entries) >= MAX_TRANSFERS:
            raise HTTPException(503, "Temporary transfer capacity reached; retry later")
        reference = secrets.token_urlsafe(32)
        transfer = Transfer(
            Path(self.directory.name) / reference,
            file_name,
            content_type,
            owner,
            purpose,
            time.time() + TRANSFER_TTL_SECONDS,
        )
        self.entries[reference] = transfer
        return reference, transfer

    def get(self, reference: str, owner: str | None, purpose: str) -> Transfer:
        self.prune()
        transfer = self.entries.get(reference)
        if (
            transfer is None
            or transfer.owner != owner
            or transfer.purpose != purpose
            or transfer.expires_at <= time.time()
            or not transfer.sha256
        ):
            raise HTTPException(404, "File reference missing, expired or inaccessible")
        return transfer

    def discard(self, reference):
        transfer = self.entries.pop(reference, None)
        if transfer:
            transfer.path.unlink(missing_ok=True)


store = TransferStore()


def tool_owner() -> str | None:
    token = get_access_token()
    return hashlib.sha256(token.token.encode()).hexdigest() if token else None


def validate_content_type(value: str) -> str:
    if (
        len(value) > 255
        or "/" not in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("content_type must be a MIME type without control characters")
    return value


async def request_owner(request: Request) -> str | None:
    # Custom FastMCP routes are not automatically protected like the MCP endpoint.
    from src.server import mcp

    if mcp.auth is None:
        return None
    scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
    token = (
        await mcp.auth.verify_token(bearer)
        if scheme.lower() == "bearer" and bearer else None
    )
    if (
        token is None
        or (token.expires_at is not None and token.expires_at <= time.time())
        or not set(mcp.auth.required_scopes).issubset(token.scopes)
    ):
        raise HTTPException(
            401, "Valid MCP bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return hashlib.sha256(token.token.encode()).hexdigest()


def register_transfer_routes(mcp):
    logging.getLogger("uvicorn.access").addFilter(DownloadAccessLogFilter())

    @mcp.custom_route("/attachments/uploads", methods=["POST"])
    @attachment_http_errors("stage")
    async def upload_file(request: Request):
        owner = await request_owner(request)
        declared_size = request.headers.get("content-length")
        if declared_size is not None:
            try:
                declared_size = int(declared_size)
            except ValueError as error:
                raise HTTPException(400, "Content-Length must be a non-negative integer") from error
            if declared_size < 0:
                raise HTTPException(400, "Content-Length must be a non-negative integer")
            if declared_size > MAX_ATTACHMENT_BYTES:
                raise HTTPException(413, "Attachment exceeds the 10 MiB limit")
        try:
            file_name = validate_attachment_filename(
                request.query_params.get("file_name", "")
            )
            content_type = validate_content_type(
                request.headers.get("content-type", "application/octet-stream")
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        reference, transfer = store.create(file_name, content_type, owner, "upload")
        digest = hashlib.sha256()
        try:
            with transfer.path.open("xb") as destination:
                async for chunk in request.stream():
                    transfer.size_bytes += len(chunk)
                    if transfer.size_bytes > MAX_ATTACHMENT_BYTES:
                        raise HTTPException(413, "Attachment exceeds the 10 MiB limit")
                    destination.write(chunk)
                    digest.update(chunk)
            if not transfer.size_bytes:
                raise HTTPException(400, "Attachment content must not be empty")
            transfer.sha256 = digest.hexdigest()
        except BaseException:
            store.discard(reference)
            raise
        return JSONResponse({
            "file_ref": reference, "file_name": file_name,
            "size_bytes": transfer.size_bytes, "sha256": transfer.sha256,
            "expires_in_seconds": TRANSFER_TTL_SECONDS,
        }, status_code=201, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/attachments/downloads/{file_ref}", methods=["GET"])
    @attachment_http_errors("fetch")
    async def download_file(request: Request):
        # The 256-bit random reference is the short-lived download credential.
        # Only authenticated MCP calls can mint references when MCP auth is on.
        transfer = store.get(request.path_params["file_ref"], None, "download")
        return FileResponse(
            transfer.path, filename=transfer.file_name, media_type=transfer.content_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )
