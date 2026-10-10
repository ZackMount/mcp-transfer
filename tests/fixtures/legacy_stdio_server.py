"""Small independent 2024-11-05 peer, for compatibility with older MCP servers."""

import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    response = {"jsonrpc": "2.0", "id": request["id"]}
    if method == "initialize":
        response["result"] = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "legacy-cheat-engine", "version": "1.0"},
        }
    elif method == "tools/list":
        response["result"] = {"tools": [{
            "name": "add", "description": "Add two numbers",
            "inputSchema": {
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
            },
        }]}
    elif method == "tools/call":
        args = request["params"]["arguments"]
        response["result"] = {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}
    elif method == "ping":
        response["result"] = {}
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}
    print(json.dumps(response), flush=True)
