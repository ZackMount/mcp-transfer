"""Per-user secrets. Windows uses DPAPI; Unix uses owner-only files."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
from threading import RLock

from .config import atomic_write


def _windows_crypt(value: bytes, protect: bool) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(value)
    incoming = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    outgoing = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(outgoing.data, outgoing.length)
    finally:
        kernel32.LocalFree(outgoing.data)


class Vault:
    def __init__(self, home: Path):
        self.path = home / "secrets.bin"
        self.lock = RLock()

    def read(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return {}
            raw = self.path.read_bytes()
            if os.name == "nt":
                raw = _windows_crypt(raw, False)
            return json.loads(raw)

    def update(self, **values):
        with self.lock:
            data = self.read()
            data.update(values)
            raw = json.dumps(data, ensure_ascii=False).encode()
            atomic_write(self.path, _windows_crypt(raw, True) if os.name == "nt" else raw)


def secure_windows_directory(path: Path):
    if os.name == "nt":
        import subprocess

        identity = subprocess.check_output(["whoami"], text=True).strip()
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{identity}:(OI)(CI)F",
                "SYSTEM:(OI)(CI)F",
                "/Q",
            ],
            check=True,
            capture_output=True,
        )
        # Only protect the directory's DACL. Applying /inheritance:r recursively
        # removes inherited access from files as well. Reset descendants to the
        # now-private parent ACL, including files created before enrollment.
        for child in path.iterdir():
            subprocess.run(["icacls", str(child), "/reset", "/T", "/Q"], check=True, capture_output=True)
