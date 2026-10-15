"""The double-click installer zips in install/ match their sources and keep the modes."""
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_install_zips_are_up_to_date():
    result = subprocess.run([sys.executable, str(ROOT / "tools/build_install_zips.py"), "--check"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_macos_command_is_executable_in_zip():
    with zipfile.ZipFile(ROOT / "install/Install-Observer-macOS.zip") as zf:
        (info,) = zf.infolist()
        assert info.filename == "Install-Observer-macOS.command"
        assert info.create_system == 3
        assert (info.external_attr >> 16) & 0o111 == 0o111
        assert zf.read(info) == (ROOT / "install" / info.filename).read_bytes()


def test_windows_cmd_keeps_crlf_in_zip():
    with zipfile.ZipFile(ROOT / "install/Install-Observer-Windows.zip") as zf:
        data = zf.read("Install-Observer-Windows.cmd")
        assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
