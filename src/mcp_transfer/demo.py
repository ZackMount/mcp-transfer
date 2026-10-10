from mcp.server import MCPServer


def main():
    server = MCPServer("MCP Transfer demo")

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers without accessing files or external services."""
        return a + b

    @server.tool()
    def echo(text: str) -> str:
        """Return a string for connectivity testing."""
        return text

    server.run(transport="stdio")
