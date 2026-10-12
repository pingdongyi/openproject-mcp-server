"""Attachment tools that transfer content through MCP, including remote servers."""

import base64
import binascii
import hashlib
from typing import Annotated, Any, Dict, Optional

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, field_validator, model_validator

from src.client import MAX_ATTACHMENT_BYTES, validate_attachment_filename
from src.server import get_client, mcp

PositiveId = Annotated[int, Field(gt=0)]
MAX_BASE64_LENGTH = 4 * ((MAX_ATTACHMENT_BYTES + 2) // 3)


class UploadWorkPackageAttachmentInput(BaseModel):
    """Provide exactly one UTF-8 text or standard Base64 payload."""

    work_package_id: PositiveId
    file_name: str = Field(min_length=1, max_length=255)
    content: Optional[str] = Field(
        None,
        max_length=MAX_ATTACHMENT_BYTES,
        description="UTF-8 text content; use content_base64 for exact binary bytes.",
    )
    content_base64: Optional[str] = Field(
        None,
        max_length=MAX_BASE64_LENGTH,
        description="Standard Base64, without whitespace or a data URL prefix.",
    )
    content_type: Optional[str] = Field(
        None, max_length=255, description="MIME type; inferred from file_name if omitted."
    )
    description: Optional[str] = Field(None, max_length=10000)

    @field_validator("file_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_attachment_filename(value)

    @field_validator("content_type")
    @classmethod
    def validate_type(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and (
            "/" not in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(
                "content_type must be a MIME type without control characters"
            )
        return value

    @model_validator(mode="after")
    def validate_payload(self) -> "UploadWorkPackageAttachmentInput":
        if (self.content is None) == (self.content_base64 is None):
            raise ValueError("Provide exactly one of content or content_base64")
        self.decoded_content()
        return self

    def decoded_content(self) -> bytes:
        """Decode strictly and enforce the limit on bytes, rather than characters."""
        if self.content is not None:
            try:
                payload = self.content.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError("content must be valid UTF-8 text") from error
        else:
            try:
                payload = base64.b64decode(self.content_base64, validate=True)
            except (ValueError, binascii.Error) as error:
                raise ValueError("content_base64 must be valid standard Base64") from error
        if not payload:
            raise ValueError("Attachment content must not be empty")
        if len(payload) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachment content exceeds the 10 MiB limit")
        return payload


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
    """Upload an actual attachment to an existing ticket using text or Base64.

    No server-local file path is needed. Limited to 10 MiB of decoded content.
    Requires the ticket's attachment permission. Uploading again creates another
    attachment; list existing attachments before retrying an uncertain upload.
    uploaded_sha256 describes the sent bytes; download to verify stored bytes.
    """
    try:
        payload = input.decoded_content()
        attachment = await get_client().upload_work_package_attachment(
            input.work_package_id,
            input.file_name,
            payload,
            content_type=input.content_type,
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
async def download_attachment(
    attachment_id: PositiveId, include_content: bool = True,
) -> Dict[str, Any]:
    """Download up to 10 MiB as Base64 with a SHA-256 of the stored bytes.

    Set include_content=false for size/hash verification without a large MCP
    result. The file is still downloaded and hashed; no server-local file is saved.
    """
    try:
        client = get_client()
        metadata = await client.get_attachment(attachment_id)
        payload = await client.download_attachment(attachment_id)
        result = {
            "attachment_id": attachment_id,
            "file_name": metadata.get("fileName"),
            "content_type": metadata.get("contentType"),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        if include_content:
            result["content_base64"] = base64.b64encode(payload).decode("ascii")
        return result
    except Exception as error:
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
