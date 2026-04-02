# FortiGate bulk upgrade tools (SSH + web)

> **Warning — use at your own risk**  
> These scripts can back up configs and trigger firmware upgrades. Upgrades can reboot devices, cause downtime, or fail if the image or path is wrong. Test in a lab first. You are responsible for impact.

---

## Two ways to work

| Script | When to use | Needs |
|--------|-------------|--------|
| **`web_upgrade_assist.py`** | You only have **web login** (`https://IP:8080`) | Chrome, Selenium, `branches.csv` |
| **`main.py`** | You have **SSH** to each FortiGate from the PC you run on | `paramiko`, `ips.txt`, SSH credentials |

---

## Install (once)

Use a project virtual so the editor resolves imports (e.g. Selenium):

```powershell
cd "C:\Users\AKHILRAJ\Documents\projects\ssh-bulk-tool"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Cursor/VS Code should use `.venv` via `.vscode/settings.json`. If imports fail, use **Python: Select Interpreter** → `.venv\Scripts\python.exe`.

---

## A) Web upgrade (browser UI)

1. Copy `branches.csv.example` to `branches.csv` and edit (do not commit real passwords to a public repo).
2. Columns: `ip`, `username`, `password`, `firmware_file` (local path or OneDrive/HTTP URL).
3. Run:

```powershell
python web_upgrade_assist.py
```

Or double-click `run_web_upgrade.bat`.

**Flow:** opens `https://ip:8080`, logs in, tries backup/firmware UI clicks, you confirm backup, uploads firmware, waits for ping, one branch at a time.

**Folders:** `firmware_cache/` (URL downloads), `web_artifacts/` (debug screenshots/HTML).

---

## B) SSH bulk (`main.py`)

1. Add FortiGate IPs to `ips.txt` (one per line; `#` comments OK).
2. Edit `main.py`: `CREDENTIALS`, `TARGET_FORTIOS_VERSION`, and real `UPGRADE_COMMAND` when ready.
3. Keep `DO_UPGRADE = False` for a dry run first.
4. Run:

```powershell
python main.py
```

Or `run_ssh.bat`.

### SSH workflow (summary)

**Phase 1 — staging:** ping → SSH → FortiGate detection → dual backups (`show full-configuration`, `show`) → eligibility.

**Phase 2 — upgrade:** only if `DO_UPGRADE = True`; rolling (default one device at a time), cooldown, optional stop-on-failure, optional ping after upgrade.

**Outputs:** `output.log`, `summary.csv`, `backups/`.

### Safety notes

- Backups are required before upgrade in the flow.
- `main.py` refuses major/minor jumps when `TARGET_FORTIOS_VERSION` is set.
- Replace `UPGRADE_COMMAND` with your approved method (TFTP, HTTP, FortiManager, etc.); `execute update-now` is only a placeholder.

---

## Security

- Treat `branches.csv` and credentials in `main.py` as secrets.
- Do not push real passwords. See `.gitignore` for logs, backups, and caches.

---

## Disclaimer

Provided as-is. Prefer FortiManager or your org’s standard process for production where applicable.
