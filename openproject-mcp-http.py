#!/usr/bin/env python3
"""
OpenProject MCP Server - HTTP Transport Entry Point

This is the entry point for HTTP transport (claude.com + 12 users).
Includes API Key authentication for user tracking.
"""

import os
from src.server import mcp
from src.auth import APIKeyAuthProvider, load_api_keys_from_env

if __name__ == "__main__":
    # Load API keys for authentication
    api_keys = load_api_keys_from_env()
    if api_keys:
        mcp.auth = APIKeyAuthProvider(api_keys)

    # Get host and port from environment
    host = os.getenv("MCP_HTTP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_HTTP_PORT", "8000"))

    print(f"🚀 Starting OpenProject MCP Server (HTTP)")
    print(f"   Host: {host}")
    print(f"   Port: {port}")
    print(f"   API Keys: {len(api_keys)} loaded")

    # Run with HTTP transport
    mcp.run(
        transport="http",
        host=host,
        port=port
    )
