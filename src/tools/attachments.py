"""Attachment tools using references to separate binary HTTP transfers."""

import hashlib
from typing import Annotated, Any, Dict, Optional

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.client import validate_attachment_filename
from src.server import get_client, mcp
from src.attachment_transfer import (
    TRANSFER_TTL_SECONDS, download_url, register_transfer_routes, store, tool_owner,
    validate_content_type,
)

register_transfer_routes(mcp)

PositiveId = Annotated[int, Field(gt=0)]


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
    """List a ticket's attachment metadata, IDs and download links (HAL collection)."""
    try:
        return await get_client().list_work_package_attachments(work_package_id)
    except Exception as error:
        raise ToolError(f"Failed to list attachments: {error}") from error


@mcp.tool(
    annotations={
        "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False
    }
)
async def upload_work_package_attachment(
    input: UploadWorkPackageAttachmentInput,
) -> Dict[str, Any]:
    """Attach a staged binary file to an existing ticket.

    First POST raw file bytes to /attachments/uploads?file_name=... on this
    MCP server with the same bearer token; pass its file_ref here within 15 minutes.
    File bytes never enter MCP messages. Optional file_name/content_type override
    the staged metadata. Limited to 10 MiB. Upload is not idempotent: list existing
    attachments before retrying an uncertain upload. References expire and are
    process-local; deployments must route requests to the same worker.
    """
    try:
        transfer = store.get(input.file_ref, tool_owner(), "upload")
        payload = transfer.path.read_bytes()
        attachment = await get_client().upload_work_package_attachment(
            input.work_package_id,
            input.file_name or transfer.file_name,
            payload,
            content_type=input.content_type or transfer.content_type,
            description=input.description,
        )
        return {
            "attachment": attachment,
            "uploaded_size_bytes": len(payload),
            "uploaded_sha256": hashlib.sha256(payload).hexdigest(),
        }
    except Exception as error:
        raise ToolError(f"Failed to upload attachment: {error}") from error


@mcp.tool(annotations={"readOnlyHint": True})
async def get_attachment(attachment_id: PositiveId) -> Dict[str, Any]:
    """Get attachment metadata, including its container, size and download link."""
    try:
        return await get_client().get_attachment(attachment_id)
    except Exception as error:
        raise ToolError(f"Failed to get attachment: {error}") from error


@mcp.tool(annotations={"readOnlyHint": True})
async def download_attachment(attachment_id: PositiveId) -> Dict[str, Any]:
    """Prepare a binary HTTP download and return its URL, size and SHA-256.

    Open download_url directly within 15 minutes; no Bearer token is needed.
    The URL itself grants download access: share only with intended recipients.
    No file contents are returned through MCP. Limited to 10 MiB. Temporary
    downloads are process-local. MCP_PUBLIC_URL overrides the HTTP request base
    URL for proxies; without either, only download_path is available.
    """
    reference = None
    try:
        client = get_client()
        metadata = await client.get_attachment(attachment_id)
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
        raise ToolError(f"Failed to download attachment: {error}") from error


@mcp.tool(
    annotations={
        "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True
    }
)
async def delete_attachment(attachment_id: PositiveId) -> Dict[str, Any]:
    """Permanently delete an attachment by ID. This action cannot be undone.

    Use list_work_package_attachments or get_attachment to identify the file.
    Requires permission to delete attachments in its container. Deleting an
    already missing attachment reports the API's 404 error.
    """
    try:
        await get_client().delete_attachment(attachment_id)
        return {"attachment_id": attachment_id, "deleted": True}
    except Exception as error:
        raise ToolError(f"Failed to delete attachment: {error}") from error
