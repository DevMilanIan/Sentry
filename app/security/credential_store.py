from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Protocol

from app.exceptions import AuthenticationRequiredError


class ProtectedCredentialStore(Protocol):
    def save(self, key: str, value: dict[str, Any]) -> None: ...

    def load(self, key: str) -> dict[str, Any] | None: ...

    def delete(self, key: str) -> None: ...


class WindowsDpapiCredentialStore:
    """User-bound DPAPI storage for OAuth tokens/client registration; never use in prompts."""

    def __init__(self, directory: Path, *, entropy_label: str = "options-sentinel-v1") -> None:
        if os.name != "nt":
            raise RuntimeError("Windows DPAPI credential store is available only on Windows")
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._entropy = entropy_label.encode("utf-8")
        try:
            import win32crypt  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("pywin32 is required for DPAPI credential storage") from exc
        self._win32crypt = win32crypt

    def _path(self, key: str) -> Path:
        if not key or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in key
        ):
            raise ValueError("credential key contains unsafe characters")
        return self.directory / f"{key}.dpapi"

    def save(self, key: str, value: dict[str, Any]) -> None:
        plaintext = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        encrypted = self._win32crypt.CryptProtectData(
            plaintext,
            "Options Sentinel OAuth material",
            self._entropy,
            None,
            None,
            0,
        )
        path = self._path(key)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(base64.b64encode(encrypted))
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def load(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            encrypted = base64.b64decode(path.read_bytes(), validate=True)
            plaintext = self._win32crypt.CryptUnprotectData(
                encrypted, self._entropy, None, None, 0
            )[1]
            value = json.loads(plaintext.decode("utf-8"))
        except Exception as exc:
            raise AuthenticationRequiredError(
                "protected credential material cannot be decrypted"
            ) from exc
        if not isinstance(value, dict):
            raise AuthenticationRequiredError("protected credential payload has an invalid shape")
        return value

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
