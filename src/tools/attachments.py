"""Attachment tools using references to separate binary HTTP transfers."""

import hashlib
import json
from typing import Annotated, Any, Dict, Optional

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.client import MAX_ATTACHMENT_BYTES, validate_attachment_filename
from src.attachment_errors import attachment_error
from src.server import get_client, mcp
from src.attachment_transfer import (
    TRANSFER_TTL_SECONDS, download_url, register_transfer_routes, store, tool_owner,
    validate_content_type,
)

register_transfer_routes(mcp)

PositiveId = Annotated[int, Field(gt=0)]


def attachment_metadata(attachment: Dict[str, Any]) -> Dict[str, Any]:
    """Keep attachment fields and links, excluding embedded tasks and users."""
    result = {
        key: attachment[key]
        for key in ("_type", "id", "fileName", "fileSize", "contentType", "status",
                    "createdAt", "digest")
        if key in attachment
    }
    description = attachment.get("description")
    if isinstance(description, dict):
        result["description"] = {
            key: description[key] for key in ("format", "raw") if key in description
        }
    links = attachment.get("_links", {})
    result["_links"] = {
        key: links[key]
        for key in ("self", "container", "downloadLocation", "staticDownloadLocation")
        if key in links
    }
    return result


def attachment_tool_error(error: Exception, operation: str, **kwargs) -> ToolError:
    """MCP failures stay isError=true, with a machine-readable JSON text body."""
    return ToolError(json.dumps(attachment_error(error, operation, **kwargs)))


class UploadWorkPackageAttachmentInput(BaseModel):
    """Attach a file previously staged through POST /attachments/uploads."""

    model_config = ConfigDict(extra="forbid")
    work_package_id: PositiveId
    file_ref: str = Field(min_length=1, max_length=128)
    file_name: Optional[str] = Field(None, min_length=1, max_length=255)
    content_type: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = Field(None, max_length=10000)

    @field_validator("file_name")
    @classmethod
    def validate_name(cls, value: Optional[str]) -> Optional[str]:
        return validate_attachment_filename(value) if value is not None else None

    @field_validator("content_type")
    @classmethod
    def validate_type(cls, value: Optional[str]) -> Optional[str]:
        return validate_content_type(value) if value is not None else None


@mcp.tool(annotations={"readOnlyHint": True})
async def list_work_package_attachments(work_package_id: PositiveId) -> Dict[str, Any]:
    """List compact attachment metadata and IDs for a work package (HAL collection).

    Returns total/count, _embedded.elements and collection/pagination links.
    Elements contain id, fileName, fileSize, contentType, digest, description
    and attachment/container/download links when supplied by OpenProject.
    Embedded tasks, user profiles and rendered HTML are omitted.
    Reuse these IDs for download_attachment or delete_attachment; an already
    identified attachment does not require another list/get call.
    Execution failures are MCP tool errors containing JSON code, message and
    retryable fields, plus http_status and recovery guidance when available.
    """
    try:
        collection = await get_client().list_work_package_attachments(work_package_id)
        result = {
            key: collection[key]
            for key in ("_type", "total", "count", "pageSize", "offset", "_links")
            if key in collection
        }
        result["_embedded"] = {"elements": [
            attachment_metadata(item)
            for item in collection.get("_embedded", {}).get("elements", [])
        ]}
        return result
    except Exception as error:
        raise attachment_tool_error(error, "list") from error


@mcp.tool(
    annotations={
        "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False
    }
)
async def upload_work_package_attachment(
    input: UploadWorkPackageAttachmentInput,
) -> Dict[str, Any]:
    """Attach a staged binary file to an existing ticket.

    1. Obtain this MCP connection's public HTTP base URL from connection metadata
       or configuration, retaining any reverse proxy prefix but omitting /mcp
       or /sse. Do not use the OpenProject API URL or guess the server address.
       POST /attachments/uploads?file_name=<URL-encoded-basename> on that base.
       Send raw file bytes (not multipart, JSON or Base64); set Content-Type to
       the file MIME type, default application/octet-stream. When MCP auth is
       enabled, use the same MCP Bearer token as this connection, not the
       OpenProject API key. Keep tokens and file references private.
       HTTP 201 returns file_ref, file_name, size_bytes, sha256 and
       expires_in_seconds. Verify size/hash; staging does not attach the file.
    2. Pass file_ref and work_package_id here within 15 minutes using the same
       token and server worker. Optional file_name/content_type override staged
       metadata. File size must be 1 byte to 10 MiB. Never send file bytes in MCP.

    Returns compact attachment metadata, uploaded_size_bytes and uploaded_sha256
    (the bytes sent upstream, not an independent read-back verification).
    Each successful call creates a new attachment; references are reusable until
    expiry and are lost on restart. Deploy one worker or use sticky routing.
    Execution failures are MCP errors with JSON code, message and retryable.
    If outcome_unknown=true, list target attachments before retrying: the upload
    may already exist. A missing/expired reference requires staging again.
    """
    request_started = False
    try:
        transfer = store.get(input.file_ref, tool_owner(), "upload")
        payload = transfer.path.read_bytes()
        client = get_client()
        request_started = True
        attachment = await client.upload_work_package_attachment(
            input.work_package_id,
            input.file_name or transfer.file_name,
            payload,
            content_type=input.content_type or transfer.content_type,
            description=input.description,
        )
        return {
            "attachment": attachment_metadata(attachment),
            "uploaded_size_bytes": len(payload),
            "uploaded_sha256": hashlib.sha256(payload).hexdigest(),
        }
    except Exception as error:
        raise attachment_tool_error(error, "upload", request_started=request_started) from error


@mcp.tool(annotations={"readOnlyHint": True})
async def get_attachment(attachment_id: PositiveId) -> Dict[str, Any]:
    """Get compact attachment metadata by ID, including its container and links.

    Returns id, fileName, fileSize, contentType, digest, description and relevant
    _links when supplied by OpenProject. Embedded tasks/users and rendered HTML
    are omitted. Reuse existing list/get metadata; call only when it is missing
    or needs refreshing. To fetch file bytes, use download_attachment.
    Execution failures are MCP errors with JSON code, message and retryable.
    """
    try:
        return attachment_metadata(await get_client().get_attachment(attachment_id))
    except Exception as error:
        raise attachment_tool_error(error, "get") from error


@mcp.tool(annotations={"readOnlyHint": True})
async def download_attachment(attachment_id: PositiveId) -> Dict[str, Any]:
    """Prepare a binary HTTP download and return its URL, size and SHA-256.

    Success only prepares a server-side transfer; it does not save a client file.
    GET download_url directly within expires_in_seconds (15 minutes), save the
    bytes to the requested client directory, then verify the bytes written match
    size_bytes and SHA-256 matches sha256 before reporting download completion.
    No Bearer token is needed. The URL itself grants access: keep it private.
    If GET fails, report a prepared transfer plus the error, not a completed
    download. Expired references require calling this tool again.

    Returns attachment_id, file_name, content_type, size_bytes, sha256, file_ref,
    download_path, expires_in_seconds and download_url when a public base is known.
    MCP_PUBLIC_URL overrides the HTTP request base URL, including proxy prefixes.
    If only download_path is returned, resolve it against the configured public
    transfer base (retaining any proxy prefix); never guess a host or append to
    /mcp. If the base is unavailable, report the missing URL. Limited to 10 MiB;
    no bytes enter MCP. Transfers are process-local: use one worker or sticky
    routing. Execution failures are MCP errors with JSON code/message/retryable.
    """
    reference = None
    try:
        client = get_client()
        metadata = await client.get_attachment(attachment_id)
        if metadata.get("fileSize", 0) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachment exceeds the 10 MiB download limit")
        file_name = validate_attachment_filename(metadata.get("fileName") or f"attachment-{attachment_id}")
        content_type = validate_content_type(metadata.get("contentType") or "application/octet-stream")
        payload = await client.download_attachment(attachment_id)
        reference, transfer = store.create(file_name, content_type, None, "download")
        transfer.path.write_bytes(payload)
        transfer.size_bytes = len(payload)
        transfer.sha256 = hashlib.sha256(payload).hexdigest()
        path = f"/attachments/downloads/{reference}"
        result = {
            "attachment_id": attachment_id,
            "file_name": file_name,
            "content_type": content_type,
            "size_bytes": transfer.size_bytes,
            "sha256": transfer.sha256,
            "file_ref": reference,
            "download_path": path,
            "expires_in_seconds": TRANSFER_TTL_SECONDS,
        }
        url = download_url(path)
        if url:
            result["download_url"] = url
        return result
    except Exception as error:
        if reference is not None:
            store.discard(reference)
        raise attachment_tool_error(error, "download") from error


@mcp.tool(
    annotations={
        "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True
    }
)
async def delete_attachment(attachment_id: PositiveId) -> Dict[str, Any]:
    """Permanently delete an attachment by ID. This action cannot be undone.

    Reuse the attachment ID from prior list/get/upload results; query first only
    if the target is unclear or needs refreshing. Requires deletion permission
    in its container. Success returns {"attachment_id": <id>, "deleted": true}.
    An already missing/inaccessible attachment remains an MCP tool error with
    code=attachment_not_found and http_status=404; this is not a new deletion.
    Other execution errors contain JSON code, message and retryable. After an
    uncertain failure, query the attachment or list its container before retrying.
    """
    try:
        await get_client().delete_attachment(attachment_id)
        return {"attachment_id": attachment_id, "deleted": True}
    except Exception as error:
        raise attachment_tool_error(error, "delete") from error
