#!/usr/bin/env python3
"""Mirror the ABL identity properties of a stock-accepted DTB onto ours.

Experiment D booted this exact hardware with two boot images that differed only
in their DTB: the mainline liuqin DTB was thrown back to fastboot in 4.3s, the
stock vendor_boot `dtb-02.dtb` was handed over to the kernel. The public
Qualcomm reference code accepts both DTBs offline (hw_plat=8, subtype=0, from
the device's own socinfo lines in the oops partition), so the rejection lives
in Xiaomi-private ABL code we cannot read. A strings sweep of the exact
LinuxLoader shows the DT-selection path reads exactly these root properties:
qcom,msm-id, qcom,board-id, qcom,pmic-id, qcom,rtic-id and xiaomi,miboard-id.

We cannot enumerate the private checks, but we do not have to: whatever they
read from the root of the accepted dtb-02, we can present byte-for-byte. This
tool rewrites the boot DTB's root identity properties to the stock values read
from the accepted DTB itself (never hardcoded), and deletes the root properties
the accepted DTB does not carry. Everything below the root -- the actual
mainline device tree -- is untouched.

Like abl-symbols.py this is bootloader-facing surgery for the ABL
boot DTB only; the upstream-style DTS keeps its real model and compatible.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Root properties the ABL DT-selection path is known or suspected to read.
# Present in the stock DTB -> copied verbatim; absent -> deleted from ours.
MIRROR_PROPS = (
    "model",
    "compatible",
    "qcom,msm-id",
    "qcom,board-id",
    "qcom,pmic-id",
    "qcom,rtic-id",
    "xiaomi,miboard-id",
)

# Root properties of ours with no counterpart in the stock DTB and no job in
# the bootloader; deleted so the root property set matches the accepted DTB.
DELETE_PROPS = ("chassis-type",)

# Structural root properties that must never be mirrored: their values are
# phandles or cell sizes belonging to *our* tree.
KEEP_PROPS = ("interrupt-parent", "#address-cells", "#size-cells", "phandle")


def run_dtc(dtc, args):
    result = subprocess.run(
        [str(dtc)] + args, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise SystemExit(
            f"error: dtc {' '.join(args)} failed:\n{result.stderr.strip()}"
        )
    return result.stdout


def decode(dtc, dtb_path):
    return run_dtc(dtc, ["-I", "dtb", "-O", "dts", "-@", str(dtb_path)])


def root_region(text):
    """(start, end) character offsets of the root node's own property lines."""
    root = re.search(r"^/ \{\n", text, re.MULTILINE)
    if root is None:
        raise SystemExit("error: could not find the root node")
    start = root.end()
    child = re.compile(r"^\t[^\t\n][^\n]*\{\s*$", re.MULTILINE)
    match = child.search(text, start)
    end = match.start() if match else text.index("\n};", start)
    return start, end


def parse_props(region):
    """Ordered {name: full line} for one run of property lines."""
    props = {}
    for line in region.splitlines():
        match = re.match(r"\t([A-Za-z0-9_,.+#\-]+)(?:\s*=\s*.*)?;\s*$", line)
        if match:
            props[match.group(1)] = line
    return props


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boot-dtb", required=True)
    parser.add_argument(
        "--stock-dtb",
        required=True,
        help="the DTB the exact ABL demonstrably accepted on this hardware",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--dtc", required=True)
    args = parser.parse_args()

    dtc = Path(args.dtc)
    boot_dtb = Path(args.boot_dtb)
    stock_dtb = Path(args.stock_dtb)
    for path in (dtc, boot_dtb, stock_dtb):
        if not path.exists():
            raise SystemExit(f"error: required input is unavailable: {path}")

    stock = decode(dtc, stock_dtb)
    stock_props = parse_props(stock[slice(*root_region(stock))])
    unclassified = [
        name
        for name in stock_props
        if name not in MIRROR_PROPS and name not in KEEP_PROPS
    ]
    if unclassified:
        raise SystemExit(
            "error: stock DTB carries root properties this tool does not "
            f"classify, refusing to guess: {unclassified}"
        )
    if "qcom,msm-id" not in stock_props or "qcom,board-id" not in stock_props:
        raise SystemExit("error: stock DTB lacks the qcom identity properties")

    source = decode(dtc, boot_dtb)
    start, end = root_region(source)
    boot_props = parse_props(source[start:end])

    kept, deleted = [], []
    for name, line in boot_props.items():
        if name in KEEP_PROPS:
            kept.append(line)
        elif name in MIRROR_PROPS:
            if name not in stock_props:
                deleted.append(name)
        elif name in DELETE_PROPS:
            deleted.append(name)
        else:
            raise SystemExit(
                "error: boot DTB carries a root property this tool does not "
                f"classify, refusing to guess: {name}"
            )
    mirrored = [stock_props[name] for name in MIRROR_PROPS if name in stock_props]
    patched = (
        source[:start] + "\n".join(mirrored + kept) + "\n" + source[end:]
    )

    with tempfile.TemporaryDirectory() as tmp:
        dts_path = Path(tmp) / "boot-with-stock-ids.dts"
        dts_path.write_text(patched)
        run_dtc(dtc, ["-I", "dts", "-O", "dtb", "-@", "-o", args.output, str(dts_path)])

    # Verify the result rather than trusting the edit.
    result = decode(dtc, args.output)
    out_start, out_end = root_region(result)
    out_props = parse_props(result[out_start:out_end])
    for name in MIRROR_PROPS:
        if name in stock_props:
            if out_props.get(name) != stock_props[name]:
                raise SystemExit(
                    f"error: output root property {name} does not match the "
                    f"stock value: {out_props.get(name)!r}"
                )
        elif name in out_props:
            raise SystemExit(f"error: output still carries {name}")
    for name in DELETE_PROPS:
        if name in out_props:
            raise SystemExit(f"error: output still carries {name}")
    for line in kept:
        name = re.match(r"\t([A-Za-z0-9_,.+#\-]+)", line).group(1)
        if out_props.get(name) != line:
            raise SystemExit(f"error: structural root property changed: {name}")
    extra = set(out_props) - set(stock_props)
    extra -= {re.match(r"\t([A-Za-z0-9_,.+#\-]+)", line).group(1) for line in kept}
    if extra:
        raise SystemExit(f"error: unexpected root properties in output: {sorted(extra)}")
    # Below the root properties nothing may change: same child-node content as
    # a straight round-trip of the input.
    if result[out_end:] != source[end:]:
        raise SystemExit("error: the surgery leaked outside the root properties")

    print(f"root properties mirrored : {[n for n in MIRROR_PROPS if n in stock_props]}")
    print(f"root properties deleted  : {sorted(deleted)}")
    print(f"root properties kept     : {len(kept)} structural")
    print(f"output                   : {args.output}")


if __name__ == "__main__":
    sys.exit(main())
