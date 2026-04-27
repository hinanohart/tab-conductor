"""Unit tests for tab_conductor.secret_filter.

Covers:
- .env and .env.* variants denied
- .envrc (direnv) denied
- ~/.ssh/id_rsa denied, ~/.ssh/known_hosts allowed
- ~/.kaggle/kaggle.json denied
- ~/.aws/credentials denied
- /tmp/foo.py and /home/x/src/main.py allowed
- symlink resolution: /tmp/link -> ~/.env denied via resolve
- .. traversal: /tmp/x/../.env resolved to deny
- home=tmp_path for hermetic, filesystem-independent tests
- assert_allowed raises SecretAccessDenied for denied paths
- assert_allowed is a no-op for allowed paths
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tab_conductor.exceptions import SecretAccessDenied
from tab_conductor.secret_filter import assert_allowed, denied_reason, is_denied

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_home(tmp_path: Path) -> Path:
    """Return a fake $HOME directory rooted inside tmp_path."""
    home = tmp_path / "home" / "testuser"
    home.mkdir(parents=True, exist_ok=True)
    return home


# ---------------------------------------------------------------------------
# Basename pattern tests
# ---------------------------------------------------------------------------


class TestBasenamePatterns:
    """Deny-list glob patterns applied to file basenames."""

    def test_dot_env_denied(self, tmp_path: Path) -> None:
        """.env is denied by exact pattern match."""
        home = _fake_home(tmp_path)
        path = tmp_path / ".env"
        path.touch()
        assert is_denied(path, home=home) is True

    def test_dot_env_local_denied(self, tmp_path: Path) -> None:
        """.env.local matches .env.* pattern → denied."""
        home = _fake_home(tmp_path)
        path = tmp_path / ".env.local"
        path.touch()
        assert is_denied(path, home=home) is True

    def test_dot_env_production_denied(self, tmp_path: Path) -> None:
        """.env.production matches .env.* pattern → denied."""
        home = _fake_home(tmp_path)
        path = tmp_path / ".env.production"
        path.touch()
        assert is_denied(path, home=home) is True

    def test_dot_envrc_denied(self, tmp_path: Path) -> None:
        """.envrc (direnv config) denied by .env.* pattern."""
        # Note: .envrc matches .env* glob
        home = _fake_home(tmp_path)
        path = tmp_path / ".envrc"
        path.touch()
        assert is_denied(path, home=home) is True

    def test_pem_cert_denied(self, tmp_path: Path) -> None:
        """*.pem pattern denies TLS certificates."""
        home = _fake_home(tmp_path)
        path = tmp_path / "server.pem"
        path.touch()
        assert is_denied(path, home=home) is True

    def test_dot_key_denied(self, tmp_path: Path) -> None:
        """*.key pattern denies private key files."""
        home = _fake_home(tmp_path)
        path = tmp_path / "myapp.key"
        path.touch()
        assert is_denied(path, home=home) is True

    def test_p12_denied(self, tmp_path: Path) -> None:
        """*.p12 (PKCS#12 bundle) denied."""
        home = _fake_home(tmp_path)
        path = tmp_path / "bundle.p12"
        path.touch()
        assert is_denied(path, home=home) is True

    def test_pfx_denied(self, tmp_path: Path) -> None:
        """*.pfx (Windows certificate store) denied."""
        home = _fake_home(tmp_path)
        path = tmp_path / "cert.pfx"
        path.touch()
        assert is_denied(path, home=home) is True

    def test_regular_python_file_allowed(self, tmp_path: Path) -> None:
        """.py files are safe and allowed."""
        home = _fake_home(tmp_path)
        path = tmp_path / "main.py"
        path.touch()
        assert is_denied(path, home=home) is False

    def test_regular_json_allowed(self, tmp_path: Path) -> None:
        """Non-secret JSON files (e.g. config.json) are allowed."""
        home = _fake_home(tmp_path)
        path = tmp_path / "config.json"
        path.touch()
        assert is_denied(path, home=home) is False


# ---------------------------------------------------------------------------
# SSH directory tests
# ---------------------------------------------------------------------------


class TestSshDirectory:
    """~/.ssh/ directory rules with known_hosts exception."""

    def test_id_rsa_denied(self, tmp_path: Path) -> None:
        """~/.ssh/id_rsa denied via .ssh/ directory rule."""
        home = _fake_home(tmp_path)
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir()
        key_file = ssh_dir / "id_rsa"
        key_file.touch()
        assert is_denied(key_file, home=home) is True

    def test_id_ed25519_denied(self, tmp_path: Path) -> None:
        """~/.ssh/id_ed25519 denied via .ssh/ directory rule."""
        home = _fake_home(tmp_path)
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir()
        key_file = ssh_dir / "id_ed25519"
        key_file.touch()
        assert is_denied(key_file, home=home) is True

    def test_known_hosts_allowed(self, tmp_path: Path) -> None:
        """~/.ssh/known_hosts is explicitly allowed despite .ssh/ deny rule."""
        home = _fake_home(tmp_path)
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir()
        known = ssh_dir / "known_hosts"
        known.touch()
        assert is_denied(known, home=home) is False

    def test_ssh_config_denied(self, tmp_path: Path) -> None:
        """~/.ssh/config is inside .ssh/ dir and therefore denied."""
        home = _fake_home(tmp_path)
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir()
        config = ssh_dir / "config"
        config.touch()
        assert is_denied(config, home=home) is True


# ---------------------------------------------------------------------------
# Home-relative directory tests
# ---------------------------------------------------------------------------


class TestHomeDirs:
    """Home-relative directory prefix deny rules."""

    def test_kaggle_json_denied(self, tmp_path: Path) -> None:
        """~/.kaggle/kaggle.json denied via .kaggle/ directory rule."""
        home = _fake_home(tmp_path)
        kaggle_dir = home / ".kaggle"
        kaggle_dir.mkdir()
        kfile = kaggle_dir / "kaggle.json"
        kfile.touch()
        assert is_denied(kfile, home=home) is True

    def test_aws_credentials_denied(self, tmp_path: Path) -> None:
        """~/.aws/credentials denied via .aws/ directory rule."""
        home = _fake_home(tmp_path)
        aws_dir = home / ".aws"
        aws_dir.mkdir()
        creds = aws_dir / "credentials"
        creds.touch()
        assert is_denied(creds, home=home) is True

    def test_modal_token_denied(self, tmp_path: Path) -> None:
        """~/.modal/token.json denied via .modal/ directory rule."""
        home = _fake_home(tmp_path)
        modal_dir = home / ".modal"
        modal_dir.mkdir()
        token = modal_dir / "token.json"
        token.touch()
        assert is_denied(token, home=home) is True

    def test_gnupg_private_key_denied(self, tmp_path: Path) -> None:
        """~/.gnupg/ contents denied via .gnupg/ directory rule."""
        home = _fake_home(tmp_path)
        gnupg_dir = home / ".gnupg"
        gnupg_dir.mkdir()
        ring = gnupg_dir / "secring.gpg"
        ring.touch()
        assert is_denied(ring, home=home) is True


# ---------------------------------------------------------------------------
# Safe paths tests
# ---------------------------------------------------------------------------


class TestSafePaths:
    """Paths that should NOT be denied."""

    def test_tmp_python_file_allowed(self, tmp_path: Path) -> None:
        """/tmp/foo.py is not a secret."""
        home = _fake_home(tmp_path)
        path = tmp_path / "foo.py"
        path.touch()
        assert is_denied(path, home=home) is False

    def test_src_main_py_allowed(self, tmp_path: Path) -> None:
        """A source file in a project directory is allowed."""
        home = _fake_home(tmp_path)
        src = tmp_path / "project" / "src"
        src.mkdir(parents=True)
        main = src / "main.py"
        main.touch()
        assert is_denied(main, home=home) is False

    def test_json_output_allowed(self, tmp_path: Path) -> None:
        """A non-secret JSON output file is allowed."""
        home = _fake_home(tmp_path)
        path = tmp_path / "output.json"
        path.touch()
        assert is_denied(path, home=home) is False


# ---------------------------------------------------------------------------
# Symlink and traversal tests
# ---------------------------------------------------------------------------


class TestSymlinkAndTraversal:
    """Symlink resolution and .. traversal bypass prevention."""

    def test_symlink_to_env_denied(self, tmp_path: Path) -> None:
        """Symlink /tmp/link -> <home>/.env is resolved and denied."""
        home = _fake_home(tmp_path)
        env_file = home / ".env"
        env_file.touch()
        link = tmp_path / "innocent_link"
        link.symlink_to(env_file)
        # The link itself looks innocent but resolves to a denied target
        assert is_denied(link, home=home) is True

    def test_dotdot_traversal_denied(self, tmp_path: Path) -> None:
        """Path with .. that resolves into .env is denied."""
        home = _fake_home(tmp_path)
        env_file = home / ".env"
        env_file.touch()
        # Build a path that traverses into home via ..
        subdir = home / "subdir"
        subdir.mkdir()
        # path = home/subdir/../.env → resolves to home/.env
        crafted = subdir / ".." / ".env"
        assert is_denied(crafted, home=home) is True

    def test_symlink_to_safe_file_allowed(self, tmp_path: Path) -> None:
        """Symlink to a safe file remains allowed."""
        home = _fake_home(tmp_path)
        safe = tmp_path / "data.csv"
        safe.touch()
        link = tmp_path / "link_to_data"
        link.symlink_to(safe)
        assert is_denied(link, home=home) is False


# ---------------------------------------------------------------------------
# assert_allowed tests
# ---------------------------------------------------------------------------


class TestAssertAllowed:
    """assert_allowed raises SecretAccessDenied on denied paths."""

    def test_raises_on_denied(self, tmp_path: Path) -> None:
        """assert_allowed raises SecretAccessDenied for .env."""
        home = _fake_home(tmp_path)
        env = tmp_path / ".env"
        env.touch()
        with pytest.raises(SecretAccessDenied) as exc_info:
            assert_allowed(env, home=home)
        assert str(env.resolve()) in str(exc_info.value) or ".env" in str(exc_info.value)

    def test_no_raise_on_allowed(self, tmp_path: Path) -> None:
        """assert_allowed is a no-op for safe paths."""
        home = _fake_home(tmp_path)
        safe = tmp_path / "safe.txt"
        safe.touch()
        assert_allowed(safe, home=home)  # must not raise

    def test_denied_reason_returns_string(self, tmp_path: Path) -> None:
        """denied_reason returns a non-empty string for denied paths."""
        home = _fake_home(tmp_path)
        env = tmp_path / ".env"
        env.touch()
        reason = denied_reason(env, home=home)
        assert isinstance(reason, str)
        assert len(reason) > 0

    def test_denied_reason_returns_none_for_allowed(self, tmp_path: Path) -> None:
        """denied_reason returns None for safe paths."""
        home = _fake_home(tmp_path)
        safe = tmp_path / "report.txt"
        safe.touch()
        assert denied_reason(safe, home=home) is None

    def test_hermetic_with_tmp_home(self, tmp_path: Path) -> None:
        """Tests are hermetic: real $HOME is never touched."""
        # Use tmp_path as fake home so real ~/.ssh etc. don't interfere
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        ssh_dir = fake_home / ".ssh"
        ssh_dir.mkdir()
        id_rsa = ssh_dir / "id_rsa"
        id_rsa.touch()
        assert is_denied(id_rsa, home=fake_home) is True
        # Real known_hosts (if it exists) should not affect the result
        known = ssh_dir / "known_hosts"
        known.touch()
        assert is_denied(known, home=fake_home) is False
