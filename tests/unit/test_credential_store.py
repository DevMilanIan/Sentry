from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.exceptions import AuthenticationRequiredError
from app.security import credential_store
from app.security.credential_store import WindowsDpapiCredentialStore

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is host-specific")


def test_non_windows_has_no_plaintext_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credential_store, "os", SimpleNamespace(name="posix"))
    with pytest.raises(RuntimeError, match="only on Windows"):
        WindowsDpapiCredentialStore(Path("unused"))


@windows_only
def test_dpapi_store_round_trips_without_plaintext(tmp_path: Path) -> None:
    directory = tmp_path / "private-credentials"
    store = WindowsDpapiCredentialStore(directory)
    secret = {"access_token": "fixture-sensitive-token", "expires_in": 300}

    store.save("broker-oauth", secret)

    encrypted = (directory / "broker-oauth.dpapi").read_bytes()
    assert b"fixture-sensitive-token" not in encrypted
    assert store.load("broker-oauth") == secret
    # Reopening validates existing ACLs without changing them.
    assert WindowsDpapiCredentialStore(directory).load("broker-oauth") == secret
    store.delete("broker-oauth")
    assert store.load("broker-oauth") is None


@windows_only
def test_directory_and_file_have_explicit_protected_private_ntfs_acls(tmp_path: Path) -> None:
    import ntsecuritycon
    import win32api
    import win32security

    store = WindowsDpapiCredentialStore(tmp_path / "private-credentials")
    store.save("fixture", {"value": "fixture-only"})
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        win32api.CloseHandle(token)
    allowed = {win32security.ConvertSidToStringSid(user), "S-1-5-18", "S-1-5-32-544"}
    for path, flags in ((store.directory, 3), (store.directory / "fixture.dpapi", 0)):
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION,
        )
        assert descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
        assert descriptor.GetSecurityDescriptorOwner() == user
        dacl = descriptor.GetSecurityDescriptorDacl()
        assert dacl is not None
        assert dacl.GetAceCount() == 3
        entries = [dacl.GetAce(index) for index in range(dacl.GetAceCount())]
        assert {win32security.ConvertSidToStringSid(ace[2]) for ace in entries} == allowed
        assert all(ace[0] == (win32security.ACCESS_ALLOWED_ACE_TYPE, flags) for ace in entries)
        assert all(ace[1] == ntsecuritycon.FILE_ALL_ACCESS for ace in entries)


@windows_only
def test_preexisting_inherited_directory_is_rejected_without_acl_adoption(tmp_path: Path) -> None:
    import win32security

    directory = tmp_path / "not-private"
    directory.mkdir()
    info = win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
    before = win32security.GetNamedSecurityInfo(str(directory), win32security.SE_FILE_OBJECT, info)
    before_sddl = win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(before, 1, info)
    with pytest.raises(PermissionError, match="DACL"):
        WindowsDpapiCredentialStore(directory)
    after = win32security.GetNamedSecurityInfo(str(directory), win32security.SE_FILE_OBJECT, info)
    assert (
        win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(after, 1, info)
        == before_sddl
    )
    assert list(directory.iterdir()) == []


@windows_only
@pytest.mark.parametrize("target", ["directory", "file"])
@pytest.mark.parametrize("operation", ["load", "save", "delete"])
def test_acl_widening_fails_closed_without_repair_or_mutation(
    tmp_path: Path,
    target: str,
    operation: str,
) -> None:
    import ntsecuritycon
    import win32security

    store = WindowsDpapiCredentialStore(tmp_path / "private-credentials")
    store.save("fixture", {"value": "fixture-only"})
    path = store.directory / "fixture.dpapi"
    original_bytes = path.read_bytes()
    acl_target = store.directory if target == "directory" else path
    descriptor = win32security.GetNamedSecurityInfo(
        str(acl_target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION,
        0,
        ntsecuritycon.FILE_GENERIC_READ,
        win32security.ConvertStringSidToSid("S-1-1-0"),
    )
    win32security.SetNamedSecurityInfo(
        str(acl_target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )
    with pytest.raises(PermissionError, match="DACL"):
        if operation == "save":
            store.save("fixture", {"value": "replacement-fixture"})
        else:
            getattr(store, operation)("fixture")
    assert path.read_bytes() == original_bytes
    assert not list(store.directory.glob("*.tmp"))
    after = win32security.GetNamedSecurityInfo(
        str(acl_target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    ).GetSecurityDescriptorDacl()
    assert after is not None and after.GetAceCount() == 4


@windows_only
@pytest.mark.parametrize("key", ["../token", "token:stream", "CON", "LPT1", "", "a" * 129])
def test_unsafe_keys_are_rejected(tmp_path: Path, key: str) -> None:
    store = WindowsDpapiCredentialStore(tmp_path / "private-credentials")
    with pytest.raises(ValueError):
        store.save(key, {"value": "fixture-only"})
    assert list(store.directory.iterdir()) == []


@windows_only
def test_workspace_and_onedrive_paths_are_rejected_before_creation(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="workspaces"):
        WindowsDpapiCredentialStore(Path(__file__).absolute().parents[2] / "forbidden-credentials")
    with pytest.raises(PermissionError, match="OneDrive"):
        WindowsDpapiCredentialStore(tmp_path / "OneDrive" / "forbidden-credentials")


@windows_only
def test_onedrive_environment_alias_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OneDriveCommercial", str(tmp_path))
    with pytest.raises(PermissionError, match="OneDrive"):
        WindowsDpapiCredentialStore(tmp_path / "private-credentials")


@windows_only
def test_hardlinked_credential_is_rejected(tmp_path: Path) -> None:
    store = WindowsDpapiCredentialStore(tmp_path / "private-credentials")
    store.save("fixture", {"value": "fixture-only"})
    os.link(store.directory / "fixture.dpapi", store.directory / "alias.dpapi")
    with pytest.raises(PermissionError, match="single-link"):
        store.load("fixture")


@windows_only
def test_reparse_directory_is_rejected(tmp_path: Path) -> None:
    import subprocess

    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    # A fixture-only directory junction does not require symlink privileges.
    result = subprocess.run(  # noqa: S603
        [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    try:
        with pytest.raises(PermissionError, match="reparse"):
            WindowsDpapiCredentialStore(junction / "private-credentials")
    finally:
        junction.rmdir()
    assert list(target.iterdir()) == []


@windows_only
def test_corrupt_or_wrong_entropy_material_fails_without_plaintext_fallback(tmp_path: Path) -> None:
    store = WindowsDpapiCredentialStore(tmp_path / "private-credentials")
    store.save("fixture", {"access_token": "fixture-only"})
    different_entropy = WindowsDpapiCredentialStore(store.directory, entropy_label="wrong-fixture")
    with pytest.raises(AuthenticationRequiredError, match="cannot be decrypted") as failure:
        different_entropy.load("fixture")
    assert failure.value.__suppress_context__
    (store.directory / "fixture.dpapi").write_bytes(b"not-valid-base64!")
    with pytest.raises(AuthenticationRequiredError, match="cannot be decrypted"):
        store.load("fixture")


@windows_only
def test_exclusive_temporary_creation_never_truncates_an_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WindowsDpapiCredentialStore(tmp_path / "private-credentials")
    temporary = store.directory / ".fixture.collision.tmp"
    temporary.write_bytes(b"existing-fixture")
    monkeypatch.setattr(credential_store.secrets, "token_hex", lambda _: "collision")
    with pytest.raises(FileExistsError):
        store.save("fixture", {"value": "fixture-only"})
    assert temporary.read_bytes() == b"existing-fixture"
    assert store.load("fixture") is None


@windows_only
def test_atomic_replace_failure_preserves_prior_record_and_cleans_only_own_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WindowsDpapiCredentialStore(tmp_path / "private-credentials")
    store.save("fixture", {"value": "original-fixture"})

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("fixture replacement failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failure"):
        store.save("fixture", {"value": "new-fixture"})
    assert store.load("fixture") == {"value": "original-fixture"}
    assert not list(store.directory.glob("*.tmp"))


@windows_only
def test_simultaneous_records_use_independent_temporary_files(tmp_path: Path) -> None:
    store = WindowsDpapiCredentialStore(tmp_path / "private-credentials")
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(
            executor.map(lambda index: store.save(f"fixture-{index}", {"index": index}), range(12))
        )
    assert all(store.load(f"fixture-{index}") == {"index": index} for index in range(12))
    assert not list(store.directory.glob("*.tmp"))
