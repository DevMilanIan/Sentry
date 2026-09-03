# Machine audit

**Audited:** 2026-09-02 (America/Los_Angeles)  
**Safety conclusion:** Hardware is suitable. Host provisioning and clock remediation remain open;
do not use timestamp-sensitive broker operation yet.

| Item | Observed state | Assessment |
|---|---|---|
| OS | Windows 11 Home 25H2, 10.0.26200 build 26200.9168 | Supported target |
| CPU | AMD Ryzen 5 5600X, 6 cores / 12 threads | Sufficient |
| RAM | 63.9 GiB | Sufficient |
| GPU | NVIDIA RTX 3060 Ti, 8,192 MiB; driver 591.86 / CUDA 13.1 | Suitable for one quantized ~9B model |
| GPU baseline | 43–44 °C during idle audit | Normal baseline; load benchmark pending |
| C: | 930.5 GiB NVMe, 807.9 GiB free | Sufficient |
| D: | 232.9 GiB SATA SSD, 60.3 GiB free | Not selected for primary database |
| Host timezone | Pacific Standard Time | Store UTC; display market and user time explicitly |
| Virtualization | VBS/HVCI hypervisor active; WSL and Virtual Machine Platform features disabled | Enable WSL2; reboot required |
| Docker | Not installed | Install after WSL2 enablement |
| Ollama | Not installed | Install native Windows service and benchmark |
| Python | No system Python on `PATH`; Codex bundled CPython 3.12.13 available | Project `.venv` created from bundled runtime; install a maintained host Python for unattended service |
| Git | Available; repository initialized on `main` | Local commit identity not yet configured |
| Windows Time | Service stopped, Manual; NTP points to `time.windows.com` | Unsafe until fixed |

## Critical clock finding

A five-sample `w32tm /stripchart` at approximately 18:45 PDT showed the local host consistently
5.842–5.848 seconds slow. The latest recorded successful Windows Time synchronization was
2026-08-24. Order logic, market-event ordering, replay capture, and qualification evidence must
remain disabled until the service is automatic/running, resynchronization succeeds, and measured
offset is within the operational threshold (initially 250 ms).

## Provisioning actions pending

1. In an elevated PowerShell, enable WSL and Virtual Machine Platform, install/update WSL2 Ubuntu,
   then reboot.
2. Set Windows Time to automatic, resynchronize, and verify offset.
3. Install Docker Desktop with WSL2 backend and Ubuntu integration. Do not also install Docker
   Engine in that distribution.
4. Install Ollama natively, pull two benchmark candidates, and validate actual GPU offload with
   `ollama ps` under repeated structured-output load.
5. Configure no-sleep AC power behavior and an authenticated Task Scheduler/service startup only
   after application fault/reconciliation tests pass.

Scripts under `scripts/windows/` perform auditable checks and safe setup steps; host-elevated and
rebooting operations remain deliberately separate from application code.
