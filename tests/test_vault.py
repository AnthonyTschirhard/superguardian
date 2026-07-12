"""Tests for vault.py: CSV parsing, config validation, secret handling, and the
mount/export/commit/dismount orchestration (dismount must always run)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from superguardian.vault import (
    VaultConfig,
    VaultError,
    create_container,
    dismount,
    export_firefox,
    find_ente_password,
    mount,
    run_backup,
)


def _run(coro):
    return asyncio.run(coro)


# ── find_ente_password ──────────────────────────────────────────────────────

def test_find_ente_password_matches_url(tmp_path):
    csv_file = tmp_path / "firefox.csv"
    csv_file.write_text(
        "url,username,password\n"
        "https://github.com,me,ghpass\n"
        "https://auth.ente.io,me@example.com,entepass\n"
    )
    assert find_ente_password(str(csv_file), "ente.io") == "entepass"


def test_find_ente_password_no_match_raises(tmp_path):
    csv_file = tmp_path / "firefox.csv"
    csv_file.write_text("url,username,password\nhttps://github.com,me,ghpass\n")
    with pytest.raises(VaultError, match="No Firefox-saved login"):
        find_ente_password(str(csv_file), "ente.io")


def test_find_ente_password_alt_column_names(tmp_path):
    # firefox_decrypt's column names aren't fully pinned down yet — support
    # both the Firefox-native export schema and firefox_decrypt's own.
    csv_file = tmp_path / "firefox.csv"
    csv_file.write_text(
        "login_uri,login_user,login_password\n"
        "https://auth.ente.io,me,entepass\n"
    )
    assert find_ente_password(str(csv_file), "ente.io") == "entepass"


# ── export_firefox(): stderr must never leak into the CSV data ──────────────

class _FakeFirefoxDecryptProc:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


def test_export_firefox_keeps_stderr_warnings_out_of_csv_data(tmp_path):
    # Regression test: firefox_decrypt always logs a WARNING line to stderr
    # (e.g. "profile.ini not found"). export_firefox() must not merge that
    # into the CSV data — confirmed live, doing so makes the warning line
    # csv.DictReader's header instead of "url,user,password", silently
    # breaking every row lookup (find_ente_password never matches anything
    # despite the real row being present in the export).
    stdout = b'"url","user","password"\r\n"https://auth.ente.com","me","secret"\r\n'
    stderr = b"2026-07-12 16:18:04,190 - WARNING - profile.ini not found\n"
    fake = _FakeFirefoxDecryptProc(stdout, stderr)

    async def fake_create(*args, **kwargs):
        return fake

    out_csv = str(tmp_path / "firefox.csv")
    with patch("superguardian.vault.asyncio.create_subprocess_exec", fake_create):
        _run(export_firefox("/profile", "/decrypt.py", out_csv))

    written = open(out_csv).read()
    assert "WARNING" not in written
    assert written.startswith('"url","user","password"')
    assert find_ente_password(out_csv, "ente.com") == "secret"


def test_export_firefox_raises_with_stderr_on_failure(tmp_path):
    fake = _FakeFirefoxDecryptProc(b"", b"some real error", returncode=1)

    async def fake_create(*args, **kwargs):
        return fake

    with patch("superguardian.vault.asyncio.create_subprocess_exec", fake_create):
        with pytest.raises(VaultError, match="some real error"):
            _run(export_firefox("/profile", "/decrypt.py", str(tmp_path / "firefox.csv")))


# ── VaultConfig.from_dict ────────────────────────────────────────────────────

def test_vault_config_from_dict_ok():
    cfg = {
        "vault": {
            "container": "/home/u/vault.hc",
            "mountpoint": "/run/user/1000/sg-vault",
            "firefox_profile": "/home/u/.mozilla/firefox/x.default",
            "firefox_decrypt_path": "/home/u/tools/firefox_decrypt.py",
            "ente_login_match": "ente.io",
        }
    }
    vcfg = VaultConfig.from_dict(cfg)
    assert vcfg.container == "/home/u/vault.hc"
    assert vcfg.ente_export_dir  # defaulted, not required in config


def test_vault_config_from_dict_missing_key_raises():
    cfg = {"vault": {"container": "/home/u/vault.hc"}}
    with pytest.raises(VaultError, match="Missing vault config key"):
        VaultConfig.from_dict(cfg)


def test_vault_config_from_dict_no_vault_block_raises():
    with pytest.raises(VaultError, match="Missing vault config key"):
        VaultConfig.from_dict({})


# ── mount(): password must go via stdin, never argv ──────────────────────────

class _FakeStdin:
    def __init__(self):
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.stdin = _FakeStdin()
        self.returncode = returncode

    async def communicate(self):
        return b"", b""


def test_mount_passes_password_via_stdin_not_argv(tmp_path):
    fake = _FakeProc()
    captured_args: list[str] = []

    async def fake_create(*args, **kwargs):
        captured_args.extend(args)
        return fake

    mountpoint = str(tmp_path / "mnt")
    with patch("superguardian.vault.asyncio.create_subprocess_exec", fake_create):
        _run(mount("/home/u/vault.hc", mountpoint, "super-secret-pw"))

    assert "super-secret-pw" not in captured_args
    assert b"super-secret-pw" in fake.stdin.written


def test_mount_plain_sudo_when_no_sudo_password_given(tmp_path):
    # sudo_password=None is the vault-init-CLI path: run interactively in a
    # real terminal, so plain `sudo` (no -S) lets it prompt on /dev/tty
    # normally rather than needing a piped password.
    fake = _FakeProc()
    captured_args: list[str] = []

    async def fake_create(*args, **kwargs):
        captured_args.extend(args)
        return fake

    mountpoint = str(tmp_path / "mnt")
    with patch("superguardian.vault.asyncio.create_subprocess_exec", fake_create):
        _run(mount("/home/u/vault.hc", mountpoint, "vault-pw"))

    assert captured_args[:2] == ["sudo", "veracrypt"]
    assert "-S" not in captured_args


def test_mount_chains_sudo_password_then_vault_password_via_stdin(tmp_path):
    # Regression test: on Linux, veracrypt always needs root for mount (it
    # writes into system mount infra) and refuses to self-escalate under
    # --non-interactive — confirmed live ("Failed to obtain administrator
    # privileges"). The TUI has no controlling terminal for `sudo` to prompt
    # on, so it must drive `sudo -S`, which consumes exactly the first
    # stdin line for its own password before veracrypt's own --stdin reads
    # the rest for the vault password. Both must go via stdin, never argv.
    fake = _FakeProc()
    captured_args: list[str] = []

    async def fake_create(*args, **kwargs):
        captured_args.extend(args)
        return fake

    mountpoint = str(tmp_path / "mnt")
    with patch("superguardian.vault.asyncio.create_subprocess_exec", fake_create):
        _run(mount(
            "/home/u/vault.hc", mountpoint, "vault-pw", sudo_password="sudo-pw",
        ))

    assert captured_args[:3] == ["sudo", "-S", "veracrypt"]
    assert "vault-pw" not in captured_args
    assert "sudo-pw" not in captured_args
    written = fake.stdin.written.decode()
    lines = written.splitlines()
    assert lines[0] == "sudo-pw"  # sudo -S reads this line first
    assert lines[1] == "vault-pw"  # then veracrypt --stdin reads this one


def test_create_container_runs_veracrypt_via_sudo_not_whole_process(tmp_path):
    # Regression test: create_container() must escalate only the veracrypt
    # subprocess call via sudo, never the whole Python process — running
    # the whole process under `sudo` changes $HOME and makes config.load()
    # silently resolve root's config instead of the real user's (this is
    # not hypothetical, it's exactly what happened the first time this was
    # run: the container landed at /home/root/vault.hc).
    fake = _FakeProc()
    captured_calls: list[list[str]] = []

    async def fake_create(*args, **kwargs):
        captured_calls.append(list(args))
        return fake

    container = str(tmp_path / "vault.hc")
    with patch("superguardian.vault.asyncio.create_subprocess_exec", fake_create):
        _run(create_container(container, 100, "vault-pw"))

    assert len(captured_calls) == 2  # veracrypt create, then chown
    assert captured_calls[0][:2] == ["sudo", "veracrypt"]
    assert captured_calls[1][0] == "sudo"
    assert captured_calls[1][1] == "chown"
    assert "vault-pw" not in [arg for call in captured_calls for arg in call]


def test_create_container_refuses_to_overwrite_existing(tmp_path):
    container = tmp_path / "vault.hc"
    container.write_bytes(b"already here")
    with pytest.raises(VaultError, match="already exists"):
        _run(create_container(str(container), 100, "vault-pw"))


def test_dismount_forces_and_never_prompts_interactively():
    # Regression test: without --non-interactive --force, a volume VeraCrypt
    # considers "in use" drops into an interactive "Continue? (y/n)" prompt,
    # which hangs forever with no TTY attached — confirmed live against a
    # real mount. dismount() is always called from a `finally`, so it must
    # never be able to block indefinitely.
    fake = _FakeProc()
    captured_args: list[str] = []

    async def fake_create(*args, **kwargs):
        captured_args.extend(args)
        return fake

    with patch("superguardian.vault.asyncio.create_subprocess_exec", fake_create):
        _run(dismount("/run/user/1000/superguardian-vault"))

    assert "--non-interactive" in captured_args
    assert "--force" in captured_args or "-f" in captured_args


def test_dismount_chains_sudo_password_via_stdin():
    fake = _FakeProc()
    captured_args: list[str] = []

    async def fake_create(*args, **kwargs):
        captured_args.extend(args)
        return fake

    with patch("superguardian.vault.asyncio.create_subprocess_exec", fake_create):
        _run(dismount("/run/user/1000/superguardian-vault", sudo_password="sudo-pw"))

    assert captured_args[:3] == ["sudo", "-S", "veracrypt"]
    assert "sudo-pw" not in captured_args
    assert fake.stdin.written.decode().strip() == "sudo-pw"


# ── run_backup(): dismount must always happen ────────────────────────────────

def _vcfg() -> VaultConfig:
    return VaultConfig(
        container="/home/u/vault.hc",
        mountpoint="/run/user/1000/sg-vault",
        firefox_profile="/home/u/.mozilla/firefox/x.default",
        firefox_decrypt_path="/home/u/tools/firefox_decrypt.py",
        ente_login_match="ente.io",
        ente_export_dir="/home/u/.config/ente-cli-export",
    )


def test_run_backup_dismounts_on_success():
    with (
        patch("superguardian.vault.mount", AsyncMock()) as m_mount,
        patch("superguardian.vault.export_firefox", AsyncMock()),
        patch("superguardian.vault.find_ente_password", return_value="ente-pw"),
        patch("superguardian.vault.export_ente_otp", AsyncMock()),
        patch("superguardian.vault.git_commit", AsyncMock()),
        patch("superguardian.vault.dismount", AsyncMock(return_value=0)) as m_dismount,
    ):
        _run(run_backup(_vcfg(), "veracrypt-pw", "sudo-pw"))
    m_mount.assert_awaited_once()
    m_dismount.assert_awaited_once()


def test_run_backup_dismounts_even_when_firefox_export_fails():
    with (
        patch("superguardian.vault.mount", AsyncMock()),
        patch("superguardian.vault.export_firefox", AsyncMock(side_effect=VaultError("boom"))),
        patch("superguardian.vault.dismount", AsyncMock(return_value=0)) as m_dismount,
    ):
        with pytest.raises(VaultError, match="boom"):
            _run(run_backup(_vcfg(), "veracrypt-pw", "sudo-pw"))
    m_dismount.assert_awaited_once()


def test_run_backup_dismounts_even_when_ente_export_fails():
    with (
        patch("superguardian.vault.mount", AsyncMock()),
        patch("superguardian.vault.export_firefox", AsyncMock()),
        patch("superguardian.vault.find_ente_password", return_value="ente-pw"),
        patch("superguardian.vault.export_ente_otp", AsyncMock(side_effect=VaultError("boom"))),
        patch("superguardian.vault.git_commit", AsyncMock()) as m_commit,
        patch("superguardian.vault.dismount", AsyncMock(return_value=0)) as m_dismount,
    ):
        with pytest.raises(VaultError, match="boom"):
            _run(run_backup(_vcfg(), "veracrypt-pw", "sudo-pw"))
    m_commit.assert_not_awaited()  # must not commit a partial/failed backup
    m_dismount.assert_awaited_once()


def test_run_backup_warns_loudly_when_dismount_itself_fails():
    # Regression test: confirmed live that veracrypt dismount can fail
    # (e.g. "target is busy") even with --force. run_backup() must not
    # silently swallow that — a caller ignoring it could report "backup
    # complete" while the vault is still mounted and decrypted.
    logged: list[str] = []
    with (
        patch("superguardian.vault.mount", AsyncMock()),
        patch("superguardian.vault.export_firefox", AsyncMock()),
        patch("superguardian.vault.find_ente_password", return_value="ente-pw"),
        patch("superguardian.vault.export_ente_otp", AsyncMock()),
        patch("superguardian.vault.git_commit", AsyncMock()),
        patch("superguardian.vault.dismount", AsyncMock(return_value=1)),
    ):
        _run(run_backup(_vcfg(), "veracrypt-pw", "sudo-pw", log=logged.append))
    assert any("WARNING" in line and "still be mounted" in line for line in logged)
