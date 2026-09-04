from __future__ import annotations

import base64
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Protocol

from app.exceptions import AuthenticationRequiredError


class ProtectedCredentialStore(Protocol):
    def save(self, key: str, value: dict[str, Any]) -> None: ...

    def load(self, key: str) -> dict[str, Any] | None: ...

    def delete(self, key: str) -> None: ...


class WindowsDpapiCredentialStore:
    """User-bound DPAPI plus a protected local NTFS DACL; never use in prompts.

    Only a new, dedicated leaf directory is initialized. An existing directory
    must already meet the policy: it is never adopted by resetting its ACL.
    Administrators/SYSTEM are trusted Windows principals, not an additional
    encryption identity. DPAPI remains bound to the current Windows user.
    """

    def __init__(self, directory: Path, *, entropy_label: str = "options-sentinel-v1") -> None:
        if os.name != "nt":
            raise RuntimeError("Windows DPAPI credential store is available only on Windows")
        try:
            import ntsecuritycon  # noqa: PLC0415
            import win32api  # noqa: PLC0415
            import win32crypt  # noqa: PLC0415
            import win32file  # noqa: PLC0415
            import win32security  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("pywin32 is required for DPAPI credential storage") from exc
        self._win32api = win32api
        self._win32crypt = win32crypt
        self._win32file = win32file
        self._security = win32security
        self._full_control = ntsecuritycon.FILE_ALL_ACCESS
        self._entropy = entropy_label.encode("utf-8")
        self.directory = Path(directory)
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
        )
        try:
            self._user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            win32api.CloseHandle(token)
        self._sids = (
            self._user_sid,
            win32security.ConvertStringSidToSid("S-1-5-18"),
            win32security.ConvertStringSidToSid("S-1-5-32-544"),
        )
        self._allowed_sids = {win32security.ConvertSidToStringSid(sid) for sid in self._sids}
        self._validate_location()
        if not self.directory.exists():
            # CreateDirectory applies the protected descriptor at creation: no
            # interval exists where this new directory has an inherited DACL.
            attributes = win32security.SECURITY_ATTRIBUTES()
            attributes.SECURITY_DESCRIPTOR = self._descriptor(directory=True)
            self._win32file.CreateDirectory(str(self.directory), attributes)
        self._validate_directory()

    def _validate_location(self) -> None:
        path = self.directory
        if (
            not path.is_absolute()
            or len(path.drive) != 2
            or path.drive[1] != ":"
            or any(part in {".", ".."} or part.endswith((".", " ")) for part in path.parts[1:])
            or any(":" in part for part in path.parts[1:])
        ):
            raise PermissionError("credential directory must be an ordinary absolute local path")
        workspace = Path(__file__).absolute().parents[2]
        forbidden = [workspace]
        if (Path.cwd() / ".git").exists() or (Path.cwd() / "pyproject.toml").exists():
            forbidden.append(Path.cwd())
        for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            if value := os.environ.get(name):
                forbidden.append(Path(value).absolute())
        if any(path.is_relative_to(root) for root in forbidden) or any(
            part.casefold() == "onedrive" or part.casefold().startswith("onedrive - ")
            for part in path.parts
        ):
            raise PermissionError("credential storage must remain outside workspaces and OneDrive")
        broad_paths = {Path(path.anchor), Path.home()}
        if local_app_data := os.environ.get("LOCALAPPDATA"):
            broad_paths.add(Path(local_app_data))
        if path in broad_paths:
            raise PermissionError("credential storage requires a dedicated leaf directory")
        for ancestor in reversed((path, *path.parents)):
            try:
                attributes = ancestor.lstat()
            except FileNotFoundError:
                if ancestor == path:
                    continue
                raise PermissionError("credential directory parent must already exist") from None
            if attributes.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise PermissionError("credential paths must not contain reparse points")
            if not stat.S_ISDIR(attributes.st_mode):
                raise PermissionError("credential path is not an ordinary directory")
            if (ancestor / ".git").exists():
                raise PermissionError("credential storage must remain outside workspaces")
        if (
            self._win32file.GetDriveType(path.anchor)
            != 3  # DRIVE_FIXED, not a mapped network drive.
            or self._win32api.GetVolumeInformation(path.anchor)[4].casefold() != "ntfs"
        ):
            raise PermissionError("credential storage requires local NTFS")

    def _descriptor(self, *, directory: bool) -> Any:
        security = self._security
        dacl = security.ACL()
        flags = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE if directory else 0
        for sid in self._sids:
            dacl.AddAccessAllowedAceEx(security.ACL_REVISION, flags, self._full_control, sid)
        descriptor = security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorOwner(self._user_sid, False)
        descriptor.SetSecurityDescriptorDacl(True, dacl, False)
        descriptor.SetSecurityDescriptorControl(
            security.SE_DACL_PROTECTED, security.SE_DACL_PROTECTED
        )
        return descriptor

    def _validate_acl(self, path: Path, *, directory: bool) -> None:
        security = self._security
        descriptor = security.GetNamedSecurityInfo(
            str(path),
            security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION,
        )
        owner = descriptor.GetSecurityDescriptorOwner()
        if owner is None or security.ConvertSidToStringSid(owner) != security.ConvertSidToStringSid(
            self._user_sid
        ):
            raise PermissionError("credential storage must be owned by the current Windows user")
        if not descriptor.GetSecurityDescriptorControl()[0] & security.SE_DACL_PROTECTED:
            raise PermissionError("credential storage requires a protected DACL")
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None or dacl.GetAceCount() != len(self._allowed_sids):
            raise PermissionError("credential storage has an unexpected DACL")
        flags = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE if directory else 0
        seen: set[str] = set()
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            if (
                len(ace) != 3
                or ace[0] != (security.ACCESS_ALLOWED_ACE_TYPE, flags)
                or ace[1] != self._full_control
            ):
                raise PermissionError("credential storage has an unexpected DACL entry")
            seen.add(security.ConvertSidToStringSid(ace[2]))
        if seen != self._allowed_sids:
            raise PermissionError("credential storage permits unexpected principals")

    def _validate_directory(self) -> None:
        self._validate_location()
        self._validate_acl(self.directory, directory=True)

    def _validate_file(self, path: Path) -> bool:
        try:
            attributes = path.lstat()
        except FileNotFoundError:
            return False
        if (
            attributes.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not stat.S_ISREG(attributes.st_mode)
            or attributes.st_nlink != 1
        ):
            raise PermissionError("credential material must be an ordinary, single-link file")
        self._validate_acl(path, directory=False)
        return True

    def _path(self, key: str) -> Path:
        if (
            not key
            or len(key) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for character in key
            )
        ):
            raise ValueError("credential key contains unsafe characters")
        if key.upper() in {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
        }:
            raise ValueError("credential key is a reserved Windows filename")
        return self.directory / f"{key}.dpapi"

    def save(self, key: str, value: dict[str, Any]) -> None:
        path = self._path(key)
        self._validate_directory()
        self._validate_file(path)
        plaintext = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        try:
            encrypted = self._win32crypt.CryptProtectData(
                plaintext, "Options Sentinel OAuth material", self._entropy, None, None, 1
            )  # CRYPTPROTECT_UI_FORBIDDEN; deliberately not LOCAL_MACHINE.
        except Exception:
            raise AuthenticationRequiredError(
                "credential material could not be protected"
            ) from None
        temporary = self.directory / f".{key}.{secrets.token_hex(16)}.tmp"
        # The private parent protects the newly created file even before its
        # inherited ACL is made explicit/protected. Never chmod as an ACL substitute.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                security = self._security
                security.SetNamedSecurityInfo(
                    str(temporary),
                    security.SE_FILE_OBJECT,
                    security.DACL_SECURITY_INFORMATION
                    | security.PROTECTED_DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    self._descriptor(directory=False).GetSecurityDescriptorDacl(),
                    None,
                )
                self._validate_file(temporary)
                stream.write(base64.b64encode(encrypted))
                stream.flush()
                os.fsync(stream.fileno())
            self._validate_directory()
            self._validate_file(path)
            temporary.replace(path)
            self._validate_file(path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        self._validate_directory()
        if not self._validate_file(path):
            return None
        try:
            encrypted = base64.b64decode(path.read_bytes(), validate=True)
            plaintext = self._win32crypt.CryptUnprotectData(
                encrypted, self._entropy, None, None, 1
            )[1]
            value = json.loads(plaintext.decode("utf-8"))
        except Exception:
            raise AuthenticationRequiredError(
                "protected credential material cannot be decrypted"
            ) from None
        if not isinstance(value, dict):
            raise AuthenticationRequiredError("protected credential payload has an invalid shape")
        return value

    def delete(self, key: str) -> None:
        path = self._path(key)
        self._validate_directory()
        if self._validate_file(path):
            path.unlink()
