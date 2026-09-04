from pathlib import Path


def source() -> str:
    return Path("scripts/windows/Backup-Database.ps1").read_text(encoding="utf-8")


def test_backup_uses_fixed_private_path_local_engine_and_no_password_argv() -> None:
    script = source()
    assert "Join-Path $profileDirectory '.options-sentinel'" in script
    assert "Join-Path $privateDirectory 'backups'" in script
    assert "npipe:////./pipe/dockerDesktopLinuxEngine" in script
    assert "Get-AuthenticodeSignature -LiteralPath $dockerCli" in script
    assert "label=com.docker.compose.project=options-sentinel" in script
    assert "label=com.docker.compose.service=postgres" in script
    assert "'^[a-f0-9]{12,64}$'" in script
    assert "POSTGRES_PASSWORD" not in script
    assert "PGPASSWORD" not in script
    assert "postgresql://" not in script
    assert "config --" not in script


def test_backup_validates_permissions_before_dump_and_never_overwrites_archives() -> None:
    script = source()
    assert script.index("Assert-PrivatePath $backupDirectory") < script.index("'pg_dump'")
    assert "AreAccessRulesProtected" in script
    assert "ReparsePoint" in script
    assert "[IO.FileMode]::CreateNew" in script
    assert "nothing will be overwritten" in script
    assert "Get-FileHash -LiteralPath $destination -Algorithm SHA256" in script
    assert "partial local artifacts were preserved" in script
    assert "Remove-Item" not in script


def test_backup_verifies_its_own_archive_in_owned_isolated_database_only() -> None:
    script = source()
    assert "'sentinel_backup_verify_' + $verificationMarker" in script
    assert "--template=template0" in script
    assert "'--exit-on-error', '--single-transaction', '-d', $verificationDatabase" in script
    assert "if ($restoredTables -cne '3')" in script
    assert "SELECT version_num FROM alembic_version" in script
    assert script.index("$ownerMarker -cne") < script.index("'dropdb'")
    assert "'--maintenance-db=postgres', $verificationDatabase)" in script
    assert "--force" not in script.lower()
    assert "'dropdb', '-U', 'sentinel', '-d', 'sentinel'" not in script
    assert "$process.WaitForExit(120000)" in script
    assert "$process.Kill($true)" in script
