# Docker storage recovery checkpoint — September 4, 2026

Read-only inspection confirmed two separate disks through WSL's Windows drive view;
Codex's packaged AppData view must not be used to assume they are the same file.

| Role | Physical Windows path | Observed bytes | Last write UTC |
| --- | --- | ---: | --- |
| Preserved earlier disk | `C:\Users\leaug\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\Docker\wsl\disk\docker_data.vhdx` | 4,369,416,192 | 2026-09-04 13:20:32.5965964 |
| Current native disk | `C:\Users\leaug\AppData\Local\Docker\wsl\disk\docker_data.vhdx` | 4,072,669,184 | 2026-09-04 13:45:12.8699513 |

The earlier disk was created at 07:58:13.9381899 UTC. The current disk is active and
its size/time may change. The `docker_engine`, `docker_engine_linux`, and
`dockerDesktopLinuxEngine` named pipes all reported engine
`058cd891-88ef-4490-97ee-a3958926c8cb`; no alternate old engine was found.
The running Docker WSL distribution has one 1-TB data device, `/dev/sde`, mounted at
`/mnt/docker-desktop-disk`. Its current `options-sentinel_postgres_data/_data`
directory has a September 4 13:26 UTC timestamp. No `.dump`, `.sql`, `.backup`,
`.tar`, `.tgz`, or `.zip` artifacts were found under this project's `var` directory.

This is storage-location evidence, **not proof that earlier application rows were
recovered or that the old filesystem is healthy**. No disk was mounted, copied,
repaired, deleted, or switched during this investigation.

Subsequent root preservation: the inactive earlier disk was copied to
`C:\Users\leaug\.options-sentinel\recovery\docker_data_pre_native_recovery_20260904.vhdx`.
Both original and copy have SHA256
`30b49d2a16b27b389c59fdbac8a6ad70ef6737b0e5aa8efe2ef201f32c14cd17`
and 4,369,416,192 bytes. Source modification time remained unchanged. The original
was not deleted or replaced. Neither copy has been mounted or recovered yet.

## Safe next recovery boundary

Preserve the old VHDX and continue development on the current engine. Before any
recovery, verify the old disk is inactive and make a separately named, hash-checked
copy outside packaged AppData/OneDrive. Inspect only that copy using a bounded,
explicit device-selection procedure. Do not overwrite the current Docker disk or
copy raw PostgreSQL files over its running volume. A crashed PostgreSQL directory
may require recovery on another disposable copy before producing a logical dump.

[Docker's backup guidance](https://docs.docker.com/desktop/settings-and-maintenance/backup-and-restore/)
requires Docker to be stopped before copying an active VM disk. A blanket shutdown
is not authorized by this checkpoint. [Microsoft's WSL mounting documentation](https://learn.microsoft.com/en-us/windows/wsl/wsl2-mount-disk)
supports VHD attachment with `--bare`, but specifically does not support generic
`ro` as a `wsl --mount --options` option. Do not mistake that command for enforced
read-only recovery; a copy-first procedure and explicit Linux read-only/no-journal-
replay handling need review before execution. Windows `Mount-DiskImage` and WSL's
`debugfs`/`mount`/`blockdev` are present; `Mount-VHD`, `qemu-img`, `qemu-nbd`, and
`guestfish` were not found in the inspected environments.
