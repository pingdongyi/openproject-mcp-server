"""
OpenProject MCP Server - FastMCP Implementation

Main server file that initializes FastMCP and registers all tools.
"""

import os
import logging
import ast
import pkgutil
from importlib import import_module
from dotenv import load_dotenv
from fastmcp import FastMCP

import src.tools
from src.client import OpenProjectClient

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize FastMCP server
mcp = FastMCP(
    name="openproject-mcp"
)

# Initialize OpenProject client as global variable
_client = None

try:
    base_url = os.getenv("OPENPROJECT_URL")
    api_key = os.getenv("OPENPROJECT_API_KEY")
    proxy = os.getenv("OPENPROJECT_PROXY")

    if not base_url or not api_key:
        raise ValueError(
            "Missing required environment variables: OPENPROJECT_URL and OPENPROJECT_API_KEY must be set"
        )

    _client = OpenProjectClient(
        base_url=base_url,
        api_key=api_key,
        proxy=proxy
    )

    logger.info(f"✅ OpenProject MCP Server initialized")
    logger.info(f"   Server: {base_url}")
    logger.info(f"   Proxy: {proxy if proxy else 'None'}")

except Exception as e:
    logger.error(f"❌ Failed to initialize OpenProject client: {e}")
    raise


# Dependency injection helper for tools
def get_client():
    """Get OpenProject client instance."""
    return _client


# Import ALL tool modules (decorators auto-register tools)
logger.info("Loading tool modules...")


def _discover_tool_modules():
    """Discover tool modules from the src.tools package."""
    return sorted(
        module_info.name
        for module_info in pkgutil.iter_modules(src.tools.__path__)
        if not module_info.ispkg
    )


def _is_mcp_tool_decorator(decorator):
    """Return True for @mcp.tool and @mcp.tool(...)."""
    candidate = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (
        isinstance(candidate, ast.Attribute)
        and candidate.attr == "tool"
        and isinstance(candidate.value, ast.Name)
        and candidate.value.id == "mcp"
    )


def _collect_tool_names(module):
    """Collect MCP tool function names from a tool module's source code."""
    source_path = getattr(module, "__file__", None)
    if not source_path:
        return []

    with open(source_path, encoding="utf-8") as source_file:
        tree = ast.parse(source_file.read(), filename=source_path)

    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_mcp_tool_decorator(decorator) for decorator in node.decorator_list)
    ]

try:
    tools_by_module = {}
    for module_name in _discover_tool_modules():
        module = import_module(f"src.tools.{module_name}")
        tool_names = _collect_tool_names(module)
        if tool_names:
            tools_by_module[module_name] = tool_names

    total_tools = sum(len(tool_names) for tool_names in tools_by_module.values())
    logger.info("✅ All %s tools loaded successfully", total_tools)
    for module_name, tool_names in tools_by_module.items():
        logger.info(
            "   %s (%s): %s",
            module_name,
            len(tool_names),
            ", ".join(tool_names),
        )
except ImportError as e:
    logger.warning(f"⚠️  Some tool modules failed to import: {e}")
    raise
