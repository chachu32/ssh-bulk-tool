"""FortiGate SSH workflow: ping, dual CLI backup, staged eligibility, rolling upgrade.
For browser-only access (https://IP:8080), use web_upgrade_assist.py instead."""
import paramiko
import threading
from datetime import datetime
import subprocess
import re
import os
import time
import csv
from dataclasses import dataclass
from typing import Optional

# --- Configuration ---
# Put multiple credential options here (the script will try them in order).
# TIP: keep least-privileged first; admin last.
CREDENTIALS = [
    ("your_username", "your_password"),
]

# If set, only FortiGates NOT already on this version are considered "upgradeable".
# Example: "7.0.14" or "6.4.15"
TARGET_FORTIOS_VERSION: Optional[str] = None

# Set to True to actually run the upgrade action below.
DO_UPGRADE = False

# Command used to identify FortiGate + version
DETECT_COMMAND = "get system status"

# What to run when upgrading. Keep it explicit to avoid surprises.
# NOTE: actual FortiGate upgrade workflows vary (TFTP/HTTP/USB/FortiManager).
# Keep this as a placeholder you’ll replace with your approved process.
UPGRADE_COMMAND = "execute update-now"

OUTPUT_FILE = "output.log"
SUMMARY_FILE = "summary.csv"
MAX_THREADS = 20
PING_TIMEOUT_MS = 1200
SSH_TIMEOUT_S = 7
BACKUP_DIR = "backups"
# We take *two* backups by default:
# - full: most complete text export (includes defaults)
# - running: a more compact config view
BACKUP_COMMANDS = {
    "full": "show full-configuration",
    "running": "show",
}

# Rolling upgrade controls (to prevent all branches going down together)
UPGRADE_CONCURRENCY = 1          # keep 1 to be safest
UPGRADE_COOLDOWN_S = 120         # wait between upgrades (reduce risk of simultaneous impact)
STOP_ON_UPGRADE_FAILURE = True   # if an upgrade step errors, stop further upgrades

# Optional: after sending upgrade, wait until device responds to ping again
WAIT_FOR_PING_AFTER_UPGRADE = True
PING_RECOVERY_TIMEOUT_S = 15 * 60

LOG_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()


@dataclass(frozen=True)
class DeviceInfo:
    is_fortigate: bool
    fortios_version: Optional[str]
    raw_status: str


@dataclass(frozen=True)
class StageResult:
    ip: str
    reachable: bool
    ssh_ok: bool
    is_fortigate: bool
    version: Optional[str]
    backup_ok: bool
    eligible: bool
    message: str


def ping_ok(ip: str) -> bool:
    # Windows ping: -n count, -w timeout(ms)
    try:
        r = subprocess.run(
            ["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def parse_fortios_version(system_status_output: str) -> Optional[str]:
    # Typical line examples:
    # "Version: FortiGate-VM64 v7.0.14,build0601,240109 (GA.M)"
    # "Version: FortiGate-100F v6.4.15,build2095,231215 (GA)"
    m = re.search(r"Version:\s+FortiGate-[^\s]+\s+v(\d+\.\d+\.\d+)", system_status_output)
    if m:
        return m.group(1)
    m2 = re.search(r"\bv(\d+\.\d+\.\d+)\b", system_status_output)
    return m2.group(1) if m2 else None


def detect_device(ssh: paramiko.SSHClient) -> DeviceInfo:
    stdin, stdout, stderr = ssh.exec_command(DETECT_COMMAND)
    out = (stdout.read() or b"").decode(errors="replace")
    err = (stderr.read() or b"").decode(errors="replace")
    raw = out.strip() if out.strip() else err.strip()
    is_fgt = "FortiGate" in raw or "Fortinet" in raw or "FortiOS" in raw
    ver = parse_fortios_version(raw) if is_fgt else None
    return DeviceInfo(is_fortigate=is_fgt, fortios_version=ver, raw_status=raw)


def run_command(ssh: paramiko.SSHClient, cmd: str, timeout_s: int = 60) -> tuple[str, str]:
    stdin, stdout, stderr = ssh.exec_command(cmd, get_pty=True, timeout=timeout_s)
    out = (stdout.read() or b"").decode(errors="replace")
    err = (stderr.read() or b"").decode(errors="replace")
    return out, err


def backup_fortigate_config(ip: str, ssh: paramiko.SSHClient, fortios_version: Optional[str]) -> tuple[bool, str]:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ver = fortios_version or "unknown"

    saved_paths: list[str] = []
    for label, cmd in BACKUP_COMMANDS.items():
        backup_path = os.path.join(BACKUP_DIR, f"{ip}_{ver}_{label}_{ts}.conf")
        out, err = run_command(ssh, cmd, timeout_s=240)
        content = out.strip() if out.strip() else ""
        if not content:
            return False, f"{label} backup produced no output (stderr: {err.strip()})"

        # A very small output likely means we didn't actually get the config.
        if len(content) < 200:
            return False, f"{label} backup output too small ({len(content)} bytes); refusing to trust it"

        try:
            with open(backup_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
                f.write("\n")
            saved_paths.append(backup_path)
        except Exception as e:
            return False, f"failed writing {label} backup file: {e}"

    return True, "config backups saved:\n- " + "\n- ".join(saved_paths)


def version_tuple(v: str) -> tuple[int, int, int]:
    a, b, c = v.split(".")
    return int(a), int(b), int(c)


def is_upgradeable(info: DeviceInfo) -> tuple[bool, str]:
    if not info.is_fortigate:
        return False, "not a FortiGate (detection failed)"
    if not info.fortios_version:
        return False, "FortiOS version not detected"
    if TARGET_FORTIOS_VERSION is None:
        return True, "target version not set (eligible by policy)"
    if info.fortios_version == TARGET_FORTIOS_VERSION:
        return False, f"already on target version {TARGET_FORTIOS_VERSION}"

    # Be conservative: only allow upgrade if same major.minor and patch differs,
    # to avoid accidental major/minor jumps.
    try:
        cur = version_tuple(info.fortios_version)
        tgt = version_tuple(TARGET_FORTIOS_VERSION)
    except Exception:
        return False, "version compare failed"

    if cur[0:2] != tgt[0:2]:
        return False, f"refusing major/minor jump {info.fortios_version} -> {TARGET_FORTIOS_VERSION}"
    if cur > tgt:
        return False, f"device is newer than target ({info.fortios_version} > {TARGET_FORTIOS_VERSION})"
    return True, f"upgradeable {info.fortios_version} -> {TARGET_FORTIOS_VERSION}"


def try_ssh(ip: str) -> tuple[Optional[paramiko.SSHClient], Optional[tuple[str, str]], Optional[str]]:
    last_err: Optional[str] = None
    for username, password in CREDENTIALS:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                ip,
                username=username,
                password=password,
                timeout=SSH_TIMEOUT_S,
                banner_timeout=SSH_TIMEOUT_S,
                auth_timeout=SSH_TIMEOUT_S,
                look_for_keys=False,
                allow_agent=False,
            )
            # Keepalive helps detect broken links faster.
            t = client.get_transport()
            if t is not None:
                t.set_keepalive(10)
            return client, (username, password), None
        except Exception as e:
            last_err = str(e)
            try:
                client.close()
            except Exception:
                pass
    return None, None, last_err


def stage_device(ip: str) -> StageResult:
    try:
        if not ping_ok(ip):
            msg = "SKIP: ping failed (unreachable)"
            log_output(ip, msg)
            return StageResult(ip, False, False, False, None, False, False, msg)

        client, used_cred, err = try_ssh(ip)
        if client is None:
            msg = f"SKIP: SSH login failed ({err})"
            log_output(ip, msg)
            return StageResult(ip, True, False, False, None, False, False, msg)

        info = detect_device(client)
        if not info.is_fortigate:
            msg = f"SKIP: not a FortiGate\n\n--- detection output ---\n{info.raw_status}"
            log_output(ip, msg)
            client.close()
            return StageResult(ip, True, True, False, None, False, False, msg)

        ok_bak, bak_msg = backup_fortigate_config(ip, client, info.fortios_version)
        if not ok_bak:
            msg = f"SKIP: backup failed ({bak_msg})\n\n--- system status ---\n{info.raw_status}"
            log_output(ip, msg)
            client.close()
            return StageResult(ip, True, True, True, info.fortios_version, False, False, msg)
        log_output(ip, f"BACKUP OK: {bak_msg}")

        ok, reason = is_upgradeable(info)
        if not ok:
            msg = f"SKIP: {reason}\n\n--- system status ---\n{info.raw_status}"
            log_output(ip, msg)
            client.close()
            return StageResult(ip, True, True, True, info.fortios_version, True, False, msg)

        msg = (
            "ELIGIBLE: staged for upgrade\n"
            f"- current: {info.fortios_version}\n"
            f"- target: {TARGET_FORTIOS_VERSION}\n"
            f"- user: {used_cred[0] if used_cred else 'unknown'}"
        )
        log_output(ip, msg)
        client.close()
        return StageResult(ip, True, True, True, info.fortios_version, True, True, msg)

    except Exception as e:
        msg = f"ERROR (stage): {str(e)}"
        log_output(ip, msg)
        return StageResult(ip, False, False, False, None, False, False, msg)


def wait_for_ping(ip: str, timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if ping_ok(ip):
            return True
        time.sleep(5)
    return False


def upgrade_device(ip: str) -> tuple[bool, str]:
    # Final safety checks right before anything disruptive.
    if not ping_ok(ip):
        msg = "SKIP: ping failed right before upgrade (link unstable)"
        log_output(ip, msg)
        return False, msg

    client, used_cred, err = try_ssh(ip)
    if client is None:
        msg = f"SKIP: SSH login failed right before upgrade ({err})"
        log_output(ip, msg)
        return False, msg

    try:
        info = detect_device(client)
        ok, reason = is_upgradeable(info)
        if not ok:
            msg = f"SKIP: no longer upgradeable ({reason})"
            log_output(ip, msg)
            client.close()
            return False, msg

        out, err2 = run_command(client, UPGRADE_COMMAND, timeout_s=120)
        msg = (
            "UPGRADE COMMAND SENT\n"
            f"- current: {info.fortios_version}\n"
            f"- target: {TARGET_FORTIOS_VERSION}\n"
            f"- user: {used_cred[0] if used_cred else 'unknown'}\n\n"
            f"--- stdout ---\n{out}\n\n--- stderr ---\n{err2}"
        )
        log_output(ip, msg)
        client.close()

        if WAIT_FOR_PING_AFTER_UPGRADE:
            # Device may reboot; wait until it comes back.
            back = wait_for_ping(ip, PING_RECOVERY_TIMEOUT_S)
            log_output(ip, f"POST-UPGRADE PING: {'OK' if back else 'TIMEOUT'}")
            return back, "upgrade sent; ping recovery " + ("ok" if back else "timeout")

        return True, "upgrade command sent"
    except Exception as e:
        msg = f"ERROR (upgrade): {str(e)}"
        log_output(ip, msg)
        try:
            client.close()
        except Exception:
            pass
        return False, msg


def log_output(ip, message):
    with LOG_LOCK:
        with open(OUTPUT_FILE, "a", encoding="utf-8", newline="\n") as f:
            f.write(f"[{datetime.now()}] {ip}:\n{message}\n{'-'*50}\n")


def write_summary_csv(stage_results: list[StageResult], upgrade_results: dict[str, tuple[bool, str]]):
    rows = []
    for r in sorted(stage_results, key=lambda x: x.ip):
        upgraded = r.ip in upgrade_results
        up_ok, up_msg = upgrade_results.get(r.ip, (False, ""))
        rows.append(
            {
                "ip": r.ip,
                "reachable": r.reachable,
                "ssh_ok": r.ssh_ok,
                "is_fortigate": r.is_fortigate,
                "version": r.version or "",
                "backup_ok": r.backup_ok,
                "eligible": r.eligible,
                "stage_message": r.message.replace("\n", " | "),
                "upgraded": upgraded,
                "upgrade_ok": up_ok if upgraded else "",
                "upgrade_message": up_msg.replace("\n", " | ") if upgraded else "",
            }
        )

    fieldnames = [
        "ip",
        "reachable",
        "ssh_ok",
        "is_fortigate",
        "version",
        "backup_ok",
        "eligible",
        "stage_message",
        "upgraded",
        "upgrade_ok",
        "upgrade_message",
    ]

    with open(SUMMARY_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    threads = []

    with open("ips.txt", "r") as f:
        ips = f.read().splitlines()

    # Filter blanks/comments
    ips = [ip.strip() for ip in ips if ip.strip() and not ip.strip().startswith("#")]

    # Phase 1: Stage (parallel) — reachability, SSH, detect, backups, eligibility
    sem = threading.Semaphore(MAX_THREADS)
    stage_results: list[StageResult] = []

    def stage_worker(ip: str):
        with sem:
            r = stage_device(ip)
            with STATE_LOCK:
                stage_results.append(r)

    for ip in ips:
        t = threading.Thread(target=stage_worker, args=(ip,))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    eligible = sorted([r.ip for r in stage_results if r.eligible])
    log_output("SUMMARY", f"Staged {len(stage_results)} devices; eligible for upgrade: {len(eligible)}")

    if not DO_UPGRADE:
        write_summary_csv(stage_results, {})
        print("Dry-run done! Check output.log, summary.csv and backups/")
        return

    # Phase 2: Upgrade (rolling) — limited concurrency, cooldown, stop-on-failure
    up_sem = threading.Semaphore(max(1, int(UPGRADE_CONCURRENCY)))
    failures: list[str] = []
    upgrade_results: dict[str, tuple[bool, str]] = {}
    upgrade_threads: list[threading.Thread] = []

    stop_flag = threading.Event()

    def upgrade_worker(ip: str):
        if stop_flag.is_set():
            return
        with up_sem:
            ok, msg = upgrade_device(ip)
            with STATE_LOCK:
                upgrade_results[ip] = (ok, msg)
                if not ok:
                    failures.append(ip)
                    if STOP_ON_UPGRADE_FAILURE:
                        stop_flag.set()
            # Cooldown between upgrades to avoid simultaneous reboots across branches
            time.sleep(max(0, int(UPGRADE_COOLDOWN_S)))

    for ip in eligible:
        if stop_flag.is_set():
            break
        t = threading.Thread(target=upgrade_worker, args=(ip,))
        t.start()
        upgrade_threads.append(t)

        # If concurrency is 1, this effectively becomes strictly sequential.
        if UPGRADE_CONCURRENCY == 1:
            t.join()
            if stop_flag.is_set():
                break

    for t in upgrade_threads:
        t.join()

    write_summary_csv(stage_results, upgrade_results)
    log_output("SUMMARY", f"Upgrade finished. Eligible: {len(eligible)} Failures: {len(failures)}")
    print("Done! Check output.log, summary.csv and backups/")


if __name__ == "__main__":
    main()