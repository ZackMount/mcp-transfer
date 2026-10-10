from __future__ import annotations

from fastapi import HTTPException

DEFAULT_LIMITS = {"request_concurrency": 32, "stream_concurrency": 128}


def validate_limits(body):
    result = {}
    for key in DEFAULT_LIMITS:
        value = body.get(key)
        if type(value) is not int or not 0 <= value <= 4096:
            raise ValueError("并发上限必须是 0–4096 的整数，0 表示不限制")
        result[key] = value
    return result


class RequestLimits:
    def __init__(self, settings):
        self.settings = settings
        self.active = {}

    def acquire(self, device_id, method):
        kind = "stream_concurrency" if method == "GET" else "request_concurrency"
        key = (device_id, kind)
        limit = self.settings.data.get("limits", {}).get(kind, DEFAULT_LIMITS[kind])
        count = self.active.get(key, 0)
        if limit and count >= limit:
            label = "长期 SSE 连接" if method == "GET" else "MCP 请求"
            raise HTTPException(429, f"当前设备的{label}达到并发上限 {limit}，请稍后重试或联系管理员调整云端设置", headers={"Retry-After": "1"})
        self.active[key] = count + 1
        released = False

        def release():
            nonlocal released
            if released:
                return
            released = True
            count = self.active.get(key, 1) - 1
            if count:
                self.active[key] = count
            else:
                self.active.pop(key, None)

        return release
