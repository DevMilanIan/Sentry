# Setup continuation checkpoint

## Current checkpoint: September 4, 2026

The authorized reboot and post-reboot deployment are complete. Do not repeat installers,
recreate the private environment, reset the replay cursor, or download the models again.
Actual PostgreSQL, migrations, backup/restore mechanics, app crash/restart, and database-outage
recovery have now passed. Docker/Ollama are running; the loopback dashboard is intentionally
DEMO/OFFLINE_SIM/RESEARCH/HALTED. The completed finite replay is not current-market surveillance.

Private configuration is `%LOCALAPPDATA%\OptionsSentinel\runtime.env`, outside OneDrive with
restricted NTFS ACLs. `Start-LocalStack.ps1` verifies signed local dependencies and calls the
clock/configuration-gated launcher. Ordinary startup does not build/pull images. The limited
interactive-user logon task is registered; a genuine subsequent boot/logon test remains pending.

Continue source/runtime/federal/evaluation composition and safety verification. No Robinhood
OAuth or account mapping is complete; no real qualification sessions, funding, or live trading
are authorized by the offline deployment. See the latest `DEVLOG.md` entries and `OPERATIONS.md`.
The historical pre-reboot checklist below is evidence, not an instruction to repeat its steps.

## Historical pre-reboot checkpoint

Checkpoint: September 3, 2026, late evening America/New_York. User explicitly approved the
reboot after installations. No trading, funding, brokerage OAuth, or unattended startup task
has been activated. Keep `DEMO/OFFLINE_SIM/RESEARCH` and both execution-disable gates intact.

## Installed and verified before reboot

- Windows Time Automatic/Running, resync success, five absolute offsets 136–147 ms; AC sleep
  disabled. WSL and Virtual Machine Platform both enabled with `RestartNeeded: True`.
- WSL package upgraded to 2.7.12.0 through the standard elevated WinGet installation.
- Ubuntu 24.04 LTS application package 2404.0.5.0 installed; distribution initialization not run.
- Docker Desktop 4.89.0.238018 installed in
  `C:\Users\leaug\AppData\Local\Programs\DockerDesktop`. Verified official SHA256 and Valid
  Docker Inc Authenticode. CLI 29.7.2, Compose 5.5.0. Engine is not running yet.
- Python 3.12.10 installed at
  `C:\Users\leaug\AppData\Local\Programs\Python\Python312\python.exe`.
  Existing `.venv` was preserved, not recreated; it uses bundled Python 3.12.13.
- Ollama 0.33.2 and both models remain installed. Selected model is measured `qwen3.5:4b`.

No machine-wide execution policy, firewall protection, endpoint protection, or broker safety
gate was disabled. The first Windows PowerShell 5 helper was blocked by its existing script
policy; the successful helper used the installed Microsoft-signed PowerShell 7 runtime via UAC.
Setup transcripts/status are in ignored `var/setup/`; installer artifacts are not application
code and are excluded from Docker's build context.

## Next work, in order

1. Confirm reboot actually occurred, Windows Time remains healthy, and both optional features
   are active. Use bounded subprocess timeouts for WSL/Docker probes; old `wsl --status` probes
   had hung before the update. Do not repeatedly launch unbounded probes.
2. Initialize Ubuntu and verify WSL2. Launch Docker Desktop in its per-user mode and verify
   its WSL2 engine. Do not install a second Docker Engine inside Ubuntu. The Docker agreement
   was not pre-accepted; pause for the user's legal/account choices if first-launch onboarding
   requires them. Do not buy a subscription or create an external account implicitly.
3. Generate local database/dashboard secrets without displaying them or overwriting existing
   files. `.env` did not exist at this checkpoint. Prefer storage outside the OneDrive-synced
   repository and wire the Compose environment-file path consistently before use. Keep
   credential storage outside source control; do not print rendered Compose configuration.
4. Start PostgreSQL and run
   `docker compose --profile verification run --build --rm verify` using the configured local
   environment file. The eleven actual-PostgreSQL tests are still skipped locally; the new
   container target tests private schemas and a uniquely created disposable migration database.
   Do not misreport installation or a skipped test as actual database verification.
5. Build/start the safe offline application. Verify database migrations, write health, native
   Ollama reachability from containers, finite replay persistence, API safety state, and actual
   process/container restart recovery. A halted dashboard is expected with checked-in gates.
6. Test backup/restore and logon recovery before registering the optional startup task.
   Authenticated Robinhood response mapping, market data, same-account continuity, and five
   real regular broker-shadow sessions remain later gates. Funding/Live are still locked.

## Code verification at this checkpoint

461 tests passed, eleven real-PostgreSQL cases skipped, one third-party Starlette/AnyIO
deprecation warning. Ruff and strict mypy passed. PowerShell setup scripts parsed successfully.
The Docker verification stage is opt-in; the final/default build target remains `runtime`.
Unsupported production schema overrides are rejected before migrations because shipped
revisions explicitly create `shared`, `demo`, and `live`.

Read `docs/DEVLOG.md`, `docs/OPERATIONS.md`, and the master specification at
`C:\Users\leaug\Downloads\local_options_trading_super_task.md` as needed; do not restart the
implementation or repeat completed model downloads/benchmarks merely because the host rebooted.
