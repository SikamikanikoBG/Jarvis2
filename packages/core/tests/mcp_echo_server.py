"""A tiny MCP server over stdio used by the tests (spawned as a subprocess)."""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("echo")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def echo(text: str) -> str:
    """Return the text unchanged."""
    return f"echo:{text}"


@mcp.tool()
def write(text: str) -> str:
    """Pretend to write something (no annotations → not read-only, not destructive)."""
    return f"wrote:{text}"


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
def wipe() -> str:
    """Destructive by declaration."""
    return "wiped"


@mcp.tool()
def boom() -> str:
    """Always fails."""
    raise ValueError("kaboom")


if __name__ == "__main__":
    mcp.run()
