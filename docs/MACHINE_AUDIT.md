# Machine audit

**Initial audit:** 2026-09-02; **setup update:** 2026-09-03 (America/New_York).

**Safety conclusion:** The hardware supports local inference and credential-free offline testing.
Host installation has progressed; reboot activation and a running PostgreSQL deployment remain open. This is
not evidence of unattended-service readiness or permission for broker authentication, funding,
or live trading.

| Item | Observed state | Assessment |
|---|---|---|
| OS | Windows 11 Home 25H2, 10.0.26200 build 26200.9168 | Supported target |
| CPU | AMD Ryzen 5 5600X, 6 cores / 12 threads | Sufficient |
| RAM | 64 GiB installed, approximately 63.9 GiB usable | Sufficient |
| GPU | NVIDIA RTX 3060 Ti, 8,192 MiB; driver 591.86 / CUDA 13.1 | Suitable for one quantized ~9B model |
| GPU baseline | 43–44 °C during the initial idle audit | Load benchmark now recorded below |
| C: | Initial audit: 930.5 GiB NVMe, 807.9 GiB free | Recheck free space before provisioning |
| D: | Initial audit: 232.9 GiB SATA SSD, 60.3 GiB free | Not selected for primary database |
| Host timezone | Now `Eastern Standard Time` (Windows ID, with daylight-saving support) | Application display zone is `America/New_York`; persisted event timestamps use UTC |
| Privilege | Current session is not elevated | Windows feature/service changes require an administrator |
| Virtualization | Both WSL and Virtual Machine Platform features enabled September 3 at 23:34 ET; both reported `RestartNeeded: True` | Reboot pending; a working WSL2 distribution has not been verified |
| Docker | Desktop 4.89.0 installed per-user; CLI 29.7.2 and Compose 5.5.0 verified by absolute path | Engine not started; per-user install is not detected by the current WinGet package entry |
| Ollama | Native Ollama 0.33.2 installed; `qwen3.5:9b` and `qwen3.5:4b` installed | Two completed structured-output benchmark runs are retained |
| PostgreSQL | No native cluster/service/listener provisioned by this setup; Compose database not running | Database-backed migration and recovery checks remain pending |
| Python | User-scoped Python 3.12.10 installed and verified; existing project `.venv` still uses bundled CPython 3.12.13 | Docker production runtime remains separate; refresh PATH after login |
| Git | Available; repository initialized on `main`; repository-local implementation identity configured | This does not imply changes are committed or published |
| Windows Time | Now Automatic/Running; synchronization succeeded at 23:34:12 ET; five offsets 136–147 ms | Passed initial 250 ms gate; recheck after reboot |

## Critical clock finding

The initial five-sample `w32tm /stripchart` at approximately 18:45 PDT on September 2 showed the
host 5.842–5.848 seconds slow. Its recorded last successful Windows Time synchronization was
2026-08-24. These are historical observations, not a new offset measurement. On September 3,
`W32Time` was still stopped with Manual startup.

Before timestamp-sensitive broker reads, current-market capture, or qualification evidence,
set the service to Automatic/Running, resynchronize, and measure an absolute offset no greater
than the initial 250 ms operational threshold. A timezone change does not repair clock skew.
Deterministic offline fixtures use a virtual trading clock and can still be tested without
representing their timestamps as current market observations.

## Local model evidence

`benchmarks/results/qwen35_2026-09-03_verified.json` completed at
2026-09-03T15:43:59-04:00. Each model ran 100 cases with a 4,096-token context and Q4_K_M
quantization. Model digests, individual outputs/hashes, scores, latency, and GPU telemetry are
recorded in the JSON.

| Model | JSON / reference-grounding / calibration / contradiction scores | p95 latency | Resident model VRAM | Peak sampled total GPU memory |
|---|---|---|---|---|
| `qwen3.5:9b` | 1.00 / 0.99 / 1.00 / 1.00 | 18,580 ms | 5,490,081,790 bytes | 6,973 MiB |
| `qwen3.5:4b` | 1.00 / 0.99 / 0.9778 / 1.00 | 9,107 ms | 3,128,038,521 bytes | 6,941 MiB |

Both passed the score thresholds. The verified run recommends `qwen3.5:4b`: the 9B model
exceeded the 15,000 ms p95 resource gate; both were below the 7,500 MiB peak-memory gate.
Peak sampled temperatures were 57 °C and 71 °C respectively. Total GPU-memory telemetry can
include other/resident workloads and is not equivalent to the individual model's VRAM size.
The earlier completed `qwen35_2026-09-03.json` run is retained for comparison; partial JSON files
are progress artifacts, not completed evidence.

These tests use a templated suite. Grounding checks allowed reference IDs, not semantic
entailment; the scores do not establish financial correctness or trading profitability.
The checked-in `config/model.yaml` and `.env.example` now select `qwen3.5:4b`. Check existing
local environment overrides when starting; they can retain an older model choice. No cloud
fallback is configured.

## PostgreSQL provisioning boundary

An alternative native PostgreSQL 17.11-3 download was examined in ignored `var/tools/`. The
archive's PostgreSQL executables are unsigned; none was run. The accompanying EDB installer
has a Valid Authenticode signature from EnterpriseDB Corporation, certificate thumbprint
`7BEDD1269FCCF7A5D95F18274750B79893C06C70`.

On this host even the signed installer's `--help` and `--extract-only` invocations require
elevation. The available `bsdtar` could not extract that installer. No elevation bypass was
attempted, and no database credentials, service, cluster, or listener were created. The helper
`scripts/local_postgres.ps1` is a download/signature/extraction audit utility, not a working
database provisioner. Downloaded files alone do not satisfy the PostgreSQL setup gate.

## Provisioning actions pending

The September 3 late-evening setup completed the feature and clock changes described below.
WSL was upgraded from 2.3.26.0 to 2.7.12.0, and the Ubuntu 24.04 LTS package 2404.0.5.0 was
installed but not initialized as a distribution. Docker's official installer SHA256 matched
`854626704AF28A160D5AF68B96B3E32EACF08AB397CE6C12EB02A04788D73681`; Authenticode was Valid,
signed by Docker Inc. It was installed with documented `--user --quiet --backend=wsl-2` flags.
The Docker subscription agreement was not pre-accepted with `--accept-license`; first launch
may still require user action. No Docker engine/database readiness is claimed.

Elevation used the normal UAC flow with verified Microsoft-signed PowerShell 7.6.5. Windows
PowerShell 5's Restricted script policy prevented the first helper from running; no global
execution-policy setting was changed. The second helper completed clock/feature setup;
a separate elevated helper completed WSL package installation. AC automatic sleep was disabled.
Local non-secret transcripts/status are in ignored `var/setup/host-20260903-2342.*` and
`var/setup/host-20260903-wsl-update.*`. No database or broker credentials were created.

The user explicitly approved restarting the PC after installations. See `SETUP_RESUME.md`
for the post-reboot continuation. The following original provisioning checklist is retained
as a runbook, not a claim that its remaining deployment checks have passed.

1. Review and run `scripts/windows/Enable-Platform.ps1` in an elevated PowerShell. It requests
   Windows feature changes without automatically rebooting. Reboot, then update/install WSL2
   Ubuntu using the follow-up commands it prints. Earlier `wsl --status` probes hung, so no
   successful WSL status is claimed here.
2. Review and run `scripts/windows/Configure-Reliability.ps1` elevated. It configures Windows
   Time, checks offset, and disables automatic AC sleep; it does not alter firewall or endpoint
   protection. Re-measure clock health after reboot.
3. Use `scripts/windows/Install-Dependencies.ps1` to provision missing dependencies. Docker's
   installer may require UAC/reboot. Enable its WSL2 backend and Ubuntu integration; do not also
   install Docker Engine in that distribution. Ollama is already installed.
4. Configure ignored local secrets, start the Compose PostgreSQL/application stack, then verify
   migrations, database write health, replay persistence, and crash/restart reconciliation on
   that actual deployment. See `docs/OPERATIONS.md`.
5. Verify GPU behavior under the intended unattended workload. Register the optional logon
   startup task only after host reliability and application recovery checks pass.

Scripts under `scripts/windows/` perform auditable checks and safe setup steps; host-elevated and
rebooting operations remain deliberately separate from application code.
