"""A real MCP server (stdio) used to test the MCP client end to end."""
import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("fake")
LOG = os.environ.get("FAKE_MCP_LOG")


def _log(line):
    if LOG:
        open(LOG, "a").write(line + "\n")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_items(query: str = "") -> str:
    """List items."""
    return f"items matching '{query}': widget, gadget"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def get_sneaky() -> str:
    """Named like a read but the server says it writes."""
    _log("get_sneaky")
    return "mutated"


@mcp.tool()
def create_order(amount: str, currency: str) -> str:
    """Moves money (in this fake)."""
    _log(f"create_order {amount} {currency}")
    return f"ORDER-OK:{amount}{currency}"


@mcp.tool()
def delete_everything() -> str:
    """Has no rule in the service spec, so Butler must hide it."""
    _log("delete_everything")
    return "gone"


@mcp.tool()
def get_broken() -> str:
    """Always errors."""
    raise ValueError("kaboom")


if __name__ == "__main__":
    mcp.run()
