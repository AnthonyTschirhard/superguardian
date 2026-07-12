"""
VeraCrypt-encrypted password/OTP vault: mount, export Firefox + Ente Auth
data into it, git-commit, and dismount.

The vault is a local (never synced online) git repo living inside a
VeraCrypt file container. The container itself is just a regular file —
sync it to a SAVE disc via a laptop_to_save_a_files config entry.

All subprocess-based (veracrypt, firefox_decrypt, ente, git) — no crypto
reimplemented in Python, matching this project's rsync convention.
"""
from __future__ import annotations

import asyncio
import csv
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path


class VaultError(RuntimeError):
    """Raised when a vault operation (mount, export, commit...) fails."""


async def _run(
    *cmd: str,
    stdin_data: str | None = None,
    check: bool = True,
) -> tuple[int, str]:
    """
    Runs a subprocess, optionally feeding stdin_data (e.g. a password) and
    closing stdin immediately after. Returns (returncode, combined output).
    Secrets passed via stdin_data never appear in argv, so they're not
    visible via `ps`/shell history — unlike the -p flag export_ente_otp()
    has to use (no stdin form exists for that one; documented tradeoff).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if stdin_data is not None:
        assert proc.stdin is not None
        proc.stdin.write((stdin_data + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()
    stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace")
    if check and proc.returncode != 0:
        raise VaultError(f"{cmd[0]} exited with code {proc.returncode}:\n{output}")
    return proc.returncode, output


# ── mount / dismount / create (create + git_init are one-time setup) ───────────

async def mount(container: str, mountpoint: str, password: str) -> None:
    os.makedirs(mountpoint, exist_ok=True)
    await _run(
        "veracrypt", "-t", "--non-interactive", "--stdin", "--pim=0",
        "--protect-hidden=no", container, mountpoint,
        stdin_data=password,
    )


async def dismount(mountpoint: str) -> None:
    # check=False: dismount is called from a `finally`, and we don't want a
    # failure here (e.g. "not mounted") to mask the real error being handled.
    await _run("veracrypt", "-t", "-u", mountpoint, check=False)


async def create_container(container: str, size_mb: int, password: str) -> None:
    """
    One-time setup: creates a new VeraCrypt file container. Does not mount
    it or git-init it — call mount() then git_init() separately afterward.
    """
    if Path(container).exists():
        raise VaultError(f"{container} already exists — refusing to overwrite.")
    os.makedirs(os.path.dirname(container) or ".", exist_ok=True)
    await _run(
        "veracrypt", "-t", "-c", container,
        "--encryption=AES", "--hash=sha-512", "--filesystem=Ext4",
        "--volume-type=normal", f"--size={size_mb}M",
        "--non-interactive", "--stdin", "--pim=0", "--keyfiles=",
        "--random-source=/dev/urandom",
        stdin_data=password,
    )


async def git_init(mountpoint: str) -> None:
    """One-time setup: initializes the git repo inside a freshly-mounted vault."""
    await _run("git", "-C", mountpoint, "init")
    await _run("git", "-C", mountpoint, "config", "user.email", "vault@localhost")
    await _run("git", "-C", mountpoint, "config", "user.name", "Super Guardian Vault")
    (Path(mountpoint) / ".gitkeep").touch()
    await _run("git", "-C", mountpoint, "add", "-A")
    await _run("git", "-C", mountpoint, "commit", "-m", "Initial vault commit")


# ── firefox export ───────────────────────────────────────────────────────────

async def export_firefox(profile: str, decrypt_script: str, out_csv: str) -> None:
    """
    Runs firefox_decrypt against *profile*, writing a CSV export to *out_csv*
    (expected to live inside the mounted vault).

    Firefox must be closed: firefox_decrypt reads key4.db/logins.json
    directly and NSS locks those files while Firefox holds the profile open.
    """
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    _, output = await _run(
        "python3", decrypt_script, "--format=csv", "--csv-delimiter=,", profile,
    )
    Path(out_csv).write_text(output)


# ── ente auth export ─────────────────────────────────────────────────────────

def find_ente_password(firefox_csv: str, login_match: str) -> str:
    """
    Parses a firefox_decrypt CSV export and returns the password for the row
    whose URL contains *login_match* (e.g. "ente.io"). Raises VaultError with
    a clear message if no matching row is found, rather than silently
    proceeding with an empty/wrong password.
    """
    with open(firefox_csv, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            # firefox_decrypt's CSV columns are "url","user","password" — the
            # extra .get() fallbacks are just cheap insurance against a future
            # firefox_decrypt version renaming them.
            url = row.get("url") or row.get("login_uri") or ""
            if login_match in url:
                password = row.get("password") or row.get("login_password")
                if password:
                    return password
    raise VaultError(
        f"No Firefox-saved login matching {login_match!r} found in {firefox_csv} — "
        "save your Ente Auth account login in Firefox first, or check "
        "vault.ente_login_match in config.yaml."
    )


async def export_ente_otp(password: str, export_dir: str, out_txt: str) -> None:
    """
    Pulls the latest encrypted Ente Auth export (via `ente export`, which
    writes into the directory configured by `ente account update --app auth
    --dir ...`) and decrypts it to *out_txt* — plaintext otpauth:// URIs, one
    per line — expected to live inside the mounted vault.

    Requires `ente account add` to have been run once, manually, outside
    this app, so the CLI session is already authenticated.

    Note: `ente auth decrypt` only accepts the password via -p (no stdin/
    env-var form), so it's briefly visible in `ps`/`/proc/<pid>/cmdline` to
    other local users for the duration of the subprocess call — an accepted
    tradeoff on a single-user machine.
    """
    os.makedirs(os.path.dirname(out_txt) or ".", exist_ok=True)
    await _run("ente", "export")
    exports = sorted(Path(export_dir).glob("*.txt"), key=os.path.getmtime)
    if not exports:
        raise VaultError(f"No Ente export found in {export_dir} after `ente export`.")
    latest = str(exports[-1])
    await _run("ente", "auth", "decrypt", latest, out_txt, "-p", password)


# ── git commit ───────────────────────────────────────────────────────────────

async def git_commit(mountpoint: str, message: str | None = None) -> None:
    message = message or f"backup {date.today().isoformat()}"
    await _run("git", "-C", mountpoint, "add", "-A")
    code, output = await _run(
        "git", "-C", mountpoint, "commit", "-m", message, check=False,
    )
    if code != 0 and "nothing to commit" not in output:
        raise VaultError(f"git commit failed:\n{output}")


# ── orchestration ────────────────────────────────────────────────────────────

@dataclass
class VaultConfig:
    container: str
    mountpoint: str
    firefox_profile: str
    firefox_decrypt_path: str
    ente_login_match: str
    ente_export_dir: str

    @classmethod
    def from_dict(cls, cfg: dict) -> "VaultConfig":
        v = cfg.get("vault") or {}
        missing = [
            k for k in
            ("container", "mountpoint", "firefox_profile", "firefox_decrypt_path", "ente_login_match")
            if not v.get(k)
        ]
        if missing:
            raise VaultError(
                f"Missing vault config key(s): {', '.join(missing)}. "
                "Edit the `vault:` block in config.yaml."
            )
        return cls(
            container=v["container"],
            mountpoint=v["mountpoint"],
            firefox_profile=v["firefox_profile"],
            firefox_decrypt_path=v["firefox_decrypt_path"],
            ente_login_match=v["ente_login_match"],
            ente_export_dir=v.get("ente_export_dir") or str(Path.home() / ".config" / "ente-cli-export"),
        )


async def run_backup(
    cfg: VaultConfig,
    veracrypt_password: str,
    *,
    log: Callable[[str], None] = lambda line: None,
) -> None:
    """
    Mounts the vault, exports Firefox passwords + Ente Auth OTP into it,
    commits, and always dismounts afterward — even on error, so a failure
    partway through can't leave the vault sitting decrypted.
    """
    log("Mounting vault…")
    await mount(cfg.container, cfg.mountpoint, veracrypt_password)
    try:
        firefox_csv = str(Path(cfg.mountpoint) / "passwords" / "firefox.csv")
        log("Exporting Firefox passwords…")
        await export_firefox(cfg.firefox_profile, cfg.firefox_decrypt_path, firefox_csv)

        log("Locating Ente Auth password in Firefox export…")
        ente_password = find_ente_password(firefox_csv, cfg.ente_login_match)
        try:
            otp_txt = str(Path(cfg.mountpoint) / "otp" / "ente.txt")
            log("Exporting Ente Auth OTP…")
            await export_ente_otp(ente_password, cfg.ente_export_dir, otp_txt)
        finally:
            ente_password = ""  # best-effort clear; Python strings are immutable

        log("Committing to vault git repo…")
        await git_commit(cfg.mountpoint)
        log("Backup complete.")
    finally:
        log("Dismounting vault…")
        await dismount(cfg.mountpoint)


# ── one-time setup CLI: `python -m superguardian.vault init` ───────────────────

def _main() -> None:
    import argparse
    import getpass

    from . import config as config_module

    parser = argparse.ArgumentParser(description="Super Guardian vault one-time setup")
    parser.add_argument("command", choices=["init"])
    args = parser.parse_args()

    cfg = config_module.load()
    vcfg = VaultConfig.from_dict(cfg)

    if args.command == "init":
        size_mb = (cfg.get("vault") or {}).get("size_mb", 100)
        password = getpass.getpass("New VeraCrypt password for the vault: ")
        if password != getpass.getpass("Confirm password: "):
            raise SystemExit("Passwords did not match.")
        print(f"Creating {size_mb}MB container at {vcfg.container}…")
        asyncio.run(create_container(vcfg.container, size_mb, password))
        asyncio.run(mount(vcfg.container, vcfg.mountpoint, password))
        try:
            asyncio.run(git_init(vcfg.mountpoint))
        finally:
            asyncio.run(dismount(vcfg.mountpoint))
        print(f"Vault created at {vcfg.container} and git-initialized.")


if __name__ == "__main__":
    _main()
