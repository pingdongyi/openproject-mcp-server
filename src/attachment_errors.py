"""Consistent, credential-free errors for attachment tools and HTTP transfers."""

import ssl
from typing import Any

import aiohttp
from starlette.exceptions import HTTPException

from src.client import OpenProjectAPIError


def attachment_error(
    error: Exception, operation: str, *, request_started: bool = False,
) -> dict[str, Any]:
    """Describe recovery without treating an uncertain upload as safe to retry."""
    status = None
    code = "attachment_error"
    message = "Attachment operation failed."
    retryable = False
    if isinstance(error, (OpenProjectAPIError, HTTPException)):
        status = error.status if isinstance(error, OpenProjectAPIError) else error.status_code
        code = {
            400: "invalid_input", 401: "authentication_failed",
            403: "permission_denied", 404: "attachment_not_found",
            408: "timeout", 413: "attachment_too_large", 429: "rate_limited",
        }.get(status, "upstream_error")
        retryable = status in {408, 429} or status >= 500
        if isinstance(error, OpenProjectAPIError):
            message = f"API Error {status}: {error.api_message or 'OpenProject request failed.'}"
            if 300 <= status < 400:
                code = "upstream_redirect"
                message = "OpenProject redirected the request; check OPENPROJECT_URL."
        else:
            message = str(error.detail)
            if status == 404:
                code = "file_reference_unavailable"
            elif status == 503:
                code = "transfer_capacity_reached"
    elif isinstance(error, ValueError):
        code, message = "invalid_input", str(error)
        if "10 MiB" in message:
            code = "attachment_too_large"
    else:
        # The API client wraps aiohttp failures; inspect their causes without
        # exposing request URLs, proxy credentials or local temporary paths.
        causes = []
        cause = error
        while cause is not None and all(cause is not item for item in causes):
            causes.append(cause)
            cause = cause.__cause__ or cause.__context__
        if any(isinstance(item, (ssl.SSLError, aiohttp.ClientSSLError)) for item in causes):
            code = "tls_error"
            message = "TLS authentication failed; inspect certificate trust, proxy settings and the nested exception."
        elif any(isinstance(item, TimeoutError) for item in causes):
            code, message, retryable = "timeout", "Attachment request timed out.", True
        elif any(isinstance(item, (aiohttp.ClientError, ConnectionError)) for item in causes):
            code, message, retryable = "network_error", "Attachment network request failed.", True
        elif isinstance(error, OSError):
            code, message = "storage_error", "Temporary attachment storage is unavailable."

    result = {"code": code, "message": message, "retryable": retryable}
    if status is not None:
        result["http_status"] = status
    if isinstance(error, OpenProjectAPIError) and error.error_identifier:
        result["error_identifier"] = error.error_identifier
    if operation == "upload" and request_started:
        # A timeout, 5xx or unreadable response may follow a committed upload.
        # Only an explicit rejection establishes that retrying cannot duplicate it.
        uncertain = status is None or status == 408 or status >= 500
        if uncertain:
            result.update(
                retryable=False, outcome_unknown=True,
                next_step="List the target work package's attachments before retrying; the upload may already exist.",
            )
        elif status is not None and 300 <= status < 400:
            result["next_step"] = "Check OPENPROJECT_URL and list attachments before retrying."
    if operation == "delete":
        if status == 404:
            result["next_step"] = "The API reports no accessible attachment; it may already have been deleted."
        elif retryable or status is None:
            result["next_step"] = "Query the attachment or list its container before retrying an uncertain deletion."
    if code == "file_reference_unavailable":
        result["next_step"] = "Stage or prepare the file again using the same MCP token and server worker."
    return result
