from __future__ import annotations

import json

from fastapi import HTTPException

SAFE_METHODS = {
    "initialize",
    "ping",
    "server/discover",
    "notifications/initialized",
    "notifications/cancelled",
    "notifications/progress",
}
METHODS = {
    "tools/list",
    "tools/call",
    "resources/list",
    "resources/templates/list",
    "resources/read",
    "resources/subscribe",
    "resources/unsubscribe",
    "prompts/list",
    "prompts/get",
    "completion/complete",
    "subscriptions/listen",
    "logging/setLevel",
} | SAFE_METHODS


def parse_message(body: bytes) -> dict:
    if not body:
        return {}
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(400, "Invalid JSON-RPC") from error
    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
        raise HTTPException(400, "A single JSON-RPC 2.0 message is required")
    if "method" in data and not isinstance(data["method"], str):
        raise HTTPException(400, "Invalid method")
    if not isinstance(data.get("params", {}), dict):
        raise HTTPException(400, "Invalid params")
    return data


def authorize_message(message: dict, policy: dict):
    method = message.get("method")
    if not method:
        # Old protocol client responses require a bound session; handled by the gateway.
        return
    if method not in METHODS:
        raise HTTPException(403, "Method is not enabled by this gateway")
    allowed_methods = policy.get("methods", ["*"])
    if method not in SAFE_METHODS and "*" not in allowed_methods and method not in allowed_methods:
        raise HTTPException(403, "Method permission denied")
    params = message.get("params", {})
    for methods, field, key in [
        ({"tools/call"}, "tools", "name"),
        ({"resources/read", "resources/subscribe", "resources/unsubscribe"}, "resources", "uri"),
        ({"prompts/get"}, "prompts", "name"),
    ]:
        if method in methods:
            allowed = policy.get(field, ["*"])
            if "*" not in allowed and params.get(key) not in allowed:
                raise HTTPException(403, f"{field} permission denied")


def filter_result(message: dict, response: dict, policy: dict) -> dict:
    result = response.get("result")
    if not isinstance(result, dict):
        return response
    mappings = {
        "tools/list": ("tools", "name"),
        "resources/list": ("resources", "uri"),
        "prompts/list": ("prompts", "name"),
    }
    if message.get("method") in mappings:
        field, key = mappings[message["method"]]
        allowed = policy.get(field, ["*"])
        if "*" not in allowed and isinstance(result.get(field), list):
            result[field] = [item for item in result[field] if item.get(key) in allowed]
    return response
