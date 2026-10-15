#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the liuqin WLAN firmware set from stock and upstream inputs."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


BOARD_KEY = (
    "bus=pci,vendor=17cb,device=1103,subsystem-vendor=17cb,"
    "subsystem-device=0108,qmi-chip-id=18,qmi-board-id=255"
)
ENCODER_SHA = "4a2181d16d6ff35d60773b75aaa1bbe349afea7f03d4d2d97a596b81636a9e87"
BOARD_SHA = "15811f0b799fc26a881bce02e282834cd41b77c2812443ffd931012552a7c1f9"
INPUTS = {
    "amss20.bin": "cc3e477fa698a28bdb8c8115a071893f9b2f5230de190ad525e74fd69bbb6092",
    "m3.bin": "6938b4bba268a02659ee5e16992971aa0e2fab103a4f60cbccf65e4bd8ac9836",
    "regdb.bin": "06810e85c94c8f412c4e72cb38546cac90db7fdbadd45aae00f977f862d38a7b",
    "bd_m81gf.elf": "aa8ae92d781e09db8cffa960b00c9606e4bb65d9c7a89f9b32df1bd24aee8573",
}


def verify(path, expected):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit(f"Input identity mismatch: {path.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor", type=Path, required=True)
    parser.add_argument("--base-board", type=Path, required=True,
                        help="Pinned upstream board-2.bin.zst")
    parser.add_argument("--bdencoder", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("out/wlan"))
    args = parser.parse_args()
    args.bdencoder = args.bdencoder.resolve()
    verify(args.bdencoder, ENCODER_SHA)
    verify(args.base_board, "ba583c3550d15871dbbbe1349dab64bac5badced5b6bf122aa7263f8d7df76d4")
    for name, digest in INPUTS.items():
        verify(args.vendor / name, digest)
    if args.out.exists():
        raise SystemExit("Output already exists; choose a fresh directory")

    with tempfile.TemporaryDirectory(prefix="liuqin-wlan-") as temporary:
        work = Path(temporary)
        base = work / "board-2.bin"
        with base.open("wb") as stream:
            subprocess.run(["zstd", "-qdc", str(args.base_board)], stdout=stream, check=True)
        subprocess.run(["python3", str(args.bdencoder), "-e", base.name], cwd=work, check=True)
        manifest = work / "board-2.json"
        data = json.loads(manifest.read_text())
        if not isinstance(data, list) or len(data) != 1:
            raise SystemExit("Unexpected board container structure")
        matches = [entry for entry in data[0]["board"] if BOARD_KEY in entry["names"]]
        if len(matches) != 1 or matches[0]["names"] != [BOARD_KEY]:
            raise SystemExit("Expected one unshared liuqin board entry")
        shutil.copyfile(args.vendor / "bd_m81gf.elf", work / "liuqin.elf")
        matches[0]["data"] = "liuqin.elf"
        manifest.write_text(json.dumps(data, indent=4) + "\n")
        board = work / "liuqin-board.bin"
        subprocess.run(["python3", str(args.bdencoder), "-c", manifest.name,
                        "-o", board.name], cwd=work, check=True)
        verify(board, BOARD_SHA)
        # Keep output permissions independent of the caller's umask.
        args.out.mkdir(parents=True)
        args.out.chmod(0o755)
        for revision in ("hw2.0", "hw2.1"):
            (args.out / revision).mkdir(mode=0o755)
            (args.out / revision).chmod(0o755)
        for name, source in (("amss.bin", args.vendor / "amss20.bin"),
                             ("m3.bin", args.vendor / "m3.bin"),
                             ("regdb.bin", args.vendor / "regdb.bin"),
                             ("board-2.bin", board)):
            target = args.out / "hw2.0" / name
            shutil.copyfile(source, target)
            target.chmod(0o644)
            (args.out / "hw2.1" / name).symlink_to("../hw2.0/" + name)
    print(f"WLAN firmware set: {args.out}")


if __name__ == "__main__":
    main()
