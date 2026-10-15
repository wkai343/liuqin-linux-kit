#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run with unshare -Ur python3 tests/rootfs-archive.py (no device access)."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

helper = Path(__file__).resolve().parents[1] / "build/lib/rootfs-archive.sh"
with tempfile.TemporaryDirectory(prefix="liuqin-archive-test-") as directory:
    work = Path(directory)
    source, target, archive = work / "source", work / "target", work / "root.tar.gz"
    source.mkdir()
    program = source / "program"
    program.write_bytes(b"archive fixture\n")
    program.chmod(0o751)
    os.setxattr(program, "user.archive-test", b"preserved")
    subprocess.run(["setcap", "cap_net_bind_service=ep", str(program)], check=True)
    subprocess.run(["setfacl", "-m", "u:0:r--", str(program)], check=True)
    (source / "link").symlink_to("program")
    os.link(program, source / "hardlink")
    # Restrict PATH so both compressor selection branches are exercised even
    # when the developer's environment already provides pigz.
    tools = work / "bin"; tools.mkdir()
    for name in ("id", "tar", "grep", "realpath", "mktemp", "mkdir", "rm", "chmod", "ln", "date", "nproc", "gzip"):
        (tools / name).symlink_to(shutil.which(name))
    env = dict(os.environ, PATH=str(tools))
    pigz = shutil.which("pigz")
    compressors = ["gzip"] + (["pigz"] if pigz else [])
    for compressor in compressors:
        if compressor == "pigz":(tools / "pigz").symlink_to(pigz)
        archive = work / (compressor + ".tar.gz"); target = work / compressor
        for action, root in (("pack", source), ("extract", target)):
            subprocess.run(["/bin/sh", str(helper), action, str(root), str(archive)], check=True, env=env)
        # The installer has gzip, so pigz outputs must also be valid for it.
        subprocess.run([shutil.which("gzip"), "-t", str(archive)], check=True)
        restored = target / "program"
        assert restored.read_bytes() == program.read_bytes()
        assert restored.stat().st_mode == program.stat().st_mode
        assert (restored.stat().st_uid, restored.stat().st_gid) == (program.stat().st_uid, program.stat().st_gid)
        for name in ("user.archive-test", "security.capability", "system.posix_acl_access"):
            assert os.getxattr(restored, name) == os.getxattr(program, name)
        assert (target / "link").readlink() == Path("program")
        assert restored.stat().st_ino == (target / "hardlink").stat().st_ino
        for action, root in (("extract", target), ("pack", source)):
            result = subprocess.run(["/bin/sh", str(helper), action, str(root), str(archive)],
                                    capture_output=True, env=env)
            assert result.returncode != 0
        print("PASS:", compressor, "archive preserves ownership, modes, capabilities, ACLs, xattrs and links")
    # A compressor error must neither publish nor leave an incomplete archive.
    (tools / "pigz").unlink(missing_ok=True)
    failing = tools / "pigz"; failing.write_text("#!/bin/sh\nexit 1\n"); failing.chmod(0o755)
    failed = work / "failed.tar.gz"
    result = subprocess.run(["/bin/sh", str(helper), "pack", str(source), str(failed)], capture_output=True, env=env)
    assert result.returncode != 0 and not failed.exists()
    assert not list(work.glob("failed.tar.gz.*"))
    # Reflinks preserve metadata and isolate subsequent edits from the source.
    cloned = work / "cloned"
    subprocess.run(["cp", "-a", "--reflink=auto", str(source), str(cloned)], check=True)
    for name in ("user.archive-test", "security.capability", "system.posix_acl_access"):
        assert os.getxattr(cloned / "program", name) == os.getxattr(program, name)
    assert (cloned / "program").stat().st_ino == (cloned / "hardlink").stat().st_ino
    (cloned / "program").write_bytes(b"independent edit\n")
    assert program.read_bytes() == b"archive fixture\n"
print("PASS: existing-target refusal, failed compression cleanup and copy isolation")
