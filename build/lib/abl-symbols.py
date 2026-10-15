#!/usr/bin/env python3
"""Prepare symbols for Xiaomi ABL overlays on the mainline device tree.

ABL applies stock and runtime overlays to the supplied DTB. Export the union
of labels from the stock base DTBs and overlay fixups, mapping them to an
inert node outside any bus. This keeps overlay writes away from the mainline
hardware nodes while allowing the bootloader to complete its overlay step.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SINK_NODE_NAME = "liuqin-abl-overlay-sink"
SINK_PATH = "/" + SINK_NODE_NAME
ENTRY_RE = re.compile(r"^entry\.(\d+)$")


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


def extract_block(text, node_name):
    """Return the body of a top-level node, or None when it is absent."""
    marker = re.search(rf"^\s*{re.escape(node_name)}\s*\{{\s*$", text, re.MULTILINE)
    if marker is None:
        return None
    start = marker.end()
    depth = 1
    index = start
    while index < len(text) and depth:
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return text[start : index - 1]


def block_properties(body):
    """Map property name -> raw value text for one flat node body."""
    properties = {}
    for line in body.splitlines():
        match = re.match(r"\s*([A-Za-z0-9_,.+\-]+)\s*=\s*(.*?);\s*$", line)
        if match:
            properties[match.group(1)] = match.group(2)
    return properties


def overlay_symbols(dtc, overlay_dtb):
    """Symbol names one overlay needs the base tree to export."""
    body = extract_block(decode(dtc, overlay_dtb), "__fixups__")
    if body is None:
        raise SystemExit(f"error: overlay has no __fixups__ node: {overlay_dtb}")
    names = set(block_properties(body))
    if not names:
        raise SystemExit(f"error: overlay's __fixups__ node is empty: {overlay_dtb}")
    return names


def base_dtb_symbols(dtc, base_dtb):
    """Labels a stock base DTB exports, i.e. what overlays may reference."""
    body = extract_block(decode(dtc, base_dtb), "__symbols__")
    if body is None:
        raise SystemExit(f"error: base DTB has no __symbols__ node: {base_dtb}")
    names = set(block_properties(body))
    if not names:
        raise SystemExit(f"error: base DTB's __symbols__ node is empty: {base_dtb}")
    return names


def collect_overlays(overlay_dtbs, overlay_dirs):
    """Every overlay to draw symbols from, deduplicated, in a stable order.

    Explicitly named files come first in the order they were given, then each
    directory's entry.<N> files in numeric order. The union does not depend on
    the order; only the progress output does.
    """
    overlays = []
    for raw in overlay_dtbs:
        path = Path(raw)
        if not path.is_file():
            raise SystemExit(f"error: required input is unavailable: {path}")
        overlays.append(path)
    for raw in overlay_dirs:
        directory = Path(raw)
        if not directory.is_dir():
            raise SystemExit(f"error: overlay directory is unavailable: {directory}")
        found = []
        for child in directory.iterdir():
            match = ENTRY_RE.match(child.name)
            if match and child.is_file():
                found.append((int(match.group(1)), child))
        if not found:
            raise SystemExit(
                f"error: no entry.<N> overlays found in directory: {directory}"
            )
        overlays += [path for _, path in sorted(found, key=lambda item: item[0])]
    ordered = []
    seen = set()
    for path in overlays:
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def max_phandle(text):
    values = [int(v, 16) for v in re.findall(r"phandle = <0x([0-9a-fA-F]+)>", text)]
    values += [int(v) for v in re.findall(r"phandle = <(\d+)>", text)]
    return max(values) if values else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boot-dtb", required=True)
    parser.add_argument(
        "--overlay-dtb",
        action="append",
        default=[],
        metavar="DTBO",
        help="an overlay whose __fixups__ symbols must be exported; repeat to "
        "export the union of several overlays",
    )
    parser.add_argument(
        "--overlay-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="a directory of extracted DTBO entries; every file named "
        "entry.<N> in it contributes its symbols to the union",
    )
    parser.add_argument(
        "--symbols-from-dtb",
        action="append",
        default=[],
        metavar="DTB",
        help="a stock base DTB whose every exported label joins the union; "
        "repeat for each stock SoC DTB. Needed because ABL applies overlays "
        "that are not in dtbo.img (the runtime hypervisor DTBO), whose fixups "
        "can name any label a stock base exports",
    )
    parser.add_argument(
        "--rename-node",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="rename a node so ABL's hardcoded lookup paths find it; Linux "
        "binds drivers on compatible, not on node name, so this is invisible "
        "to the kernel. Used for system-cache-controller -> cache-controller, "
        "which is the path the ABL binary looks up (/soc/cache-controller)",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--dtc", required=True)
    parser.add_argument(
        "--expect-symbols",
        type=int,
        default=0,
        help="fail unless the union over all overlays is exactly this many "
        "symbols",
    )
    parser.add_argument(
        "--sink-phandle",
        action="store_true",
        help="give the sink an explicit phandle so fragments merge into it; "
        "the default leaves it phandle-less so every fragment resolves to "
        "phandle 0, which libufdt treats as an invalid target and skips",
    )
    args = parser.parse_args()

    dtc = Path(args.dtc)
    boot_dtb = Path(args.boot_dtb)
    for path in (dtc, boot_dtb):
        if not path.exists():
            raise SystemExit(f"error: required input is unavailable: {path}")
    if not args.overlay_dtb and not args.overlay_dir and not args.symbols_from_dtb:
        raise SystemExit(
            "error: pass at least one --overlay-dtb, --overlay-dir or "
            "--symbols-from-dtb"
        )

    overlays = collect_overlays(args.overlay_dtb, args.overlay_dir)
    symbols = set()
    per_overlay = []
    for overlay in overlays:
        names = overlay_symbols(dtc, overlay)
        per_overlay.append((overlay.name, len(names)))
        symbols |= names

    per_base = []
    for raw in args.symbols_from_dtb:
        base = Path(raw)
        if not base.is_file():
            raise SystemExit(f"error: required input is unavailable: {base}")
        names = base_dtb_symbols(dtc, base)
        per_base.append((base.name, len(names)))
        symbols |= names
    # Deterministic order so the output is reproducible.
    symbols = sorted(symbols)

    if args.expect_symbols and len(symbols) != args.expect_symbols:
        raise SystemExit(
            f"error: {len(overlays)} overlays and {len(per_base)} base DTBs "
            f"need {len(symbols)} symbols in union, expected "
            f"{args.expect_symbols}"
        )

    source = decode(dtc, boot_dtb)
    if extract_block(source, "__symbols__") is not None:
        raise SystemExit("error: the boot DTB already has a __symbols__ node")

    sink_phandle = max_phandle(source) + 1

    lines = [f"\t{SINK_NODE_NAME} {{"]
    if args.sink_phandle:
        lines.append(f"\t\tphandle = <{sink_phandle:#x}>;")
    lines += ["\t};", ""]
    lines.append("\t__symbols__ {")
    for name in symbols:
        lines.append(f'\t\t{name} = "{SINK_PATH}";')
    lines.append("\t};")
    addition = "\n".join(lines) + "\n"

    closing = source.rstrip().rfind("\n};")
    if closing < 0:
        raise SystemExit("error: could not find the end of the root node")
    patched = source[: closing + 1] + addition + source[closing + 1 :]

    renames = []
    for spec in args.rename_node:
        old, sep, new = spec.partition("=")
        if not sep or not old or not new:
            raise SystemExit(f"error: --rename-node wants OLD=NEW, got: {spec}")
        marker = re.compile(rf"^(\s*){re.escape(old)}(@[0-9a-fA-F]+)? \{{$", re.MULTILINE)
        matches = marker.findall(patched)
        if len(matches) != 1:
            raise SystemExit(
                f"error: node {old} occurs {len(matches)} times, expected "
                "exactly 1"
            )
        patched = marker.sub(lambda m: f"{m.group(1)}{new}{m.group(2) or ''} {{", patched)
        renames.append((old, new))

    with tempfile.TemporaryDirectory() as tmp:
        dts_path = Path(tmp) / "boot-with-symbols.dts"
        dts_path.write_text(patched)
        run_dtc(dtc, ["-I", "dts", "-O", "dtb", "-@", "-o", args.output, str(dts_path)])

    # Verify the result rather than trusting the edit.
    result = decode(dtc, args.output)
    produced = extract_block(result, "__symbols__")
    if produced is None:
        raise SystemExit("error: the output DTB has no __symbols__ node")
    exported = block_properties(produced)
    missing = sorted(set(symbols) - set(exported))
    if missing:
        raise SystemExit(
            f"error: {len(missing)} symbols missing from output: {missing[:5]}"
        )
    extra = sorted(set(exported) - set(symbols))
    if extra:
        raise SystemExit(
            f"error: {len(extra)} unexpected symbols in output: {extra[:5]}"
        )
    mistargeted = sorted(
        name for name, value in exported.items() if value != f'"{SINK_PATH}"'
    )
    if mistargeted:
        raise SystemExit(
            f"error: {len(mistargeted)} symbols do not point at the sink: "
            f"{mistargeted[:5]}"
        )
    for old, new in renames:
        if re.search(rf"^\s*{re.escape(old)}(@[0-9a-fA-F]+)? \{{$", result, re.MULTILINE):
            raise SystemExit(f"error: node {old} survived the rename to {new}")
        if not re.search(
            rf"^\s*{re.escape(new)}(@[0-9a-fA-F]+)? \{{$", result, re.MULTILINE
        ):
            raise SystemExit(f"error: renamed node {new} is absent from the output")

    sink_body = extract_block(result, SINK_NODE_NAME)
    if sink_body is None:
        raise SystemExit("error: the output DTB has no sink node")
    if args.sink_phandle:
        rendered = f"phandle = <{sink_phandle:#x}>;"
        occurrences = result.count(rendered)
        if occurrences != 1:
            raise SystemExit(
                f"error: the sink phandle {sink_phandle:#x} appears "
                f"{occurrences} times, expected exactly 1"
            )
        if rendered not in sink_body:
            raise SystemExit("error: the sink node lost its explicit phandle")
        mode = f"absorbing (phandle {sink_phandle:#x})"
    else:
        if "phandle" in sink_body:
            raise SystemExit("error: the sink node unexpectedly gained a phandle")
        mode = "phandle-less (fragments resolve to 0 and are skipped)"

    print(f"overlays scanned : {len(overlays)}")
    if per_overlay:
        smallest = min(per_overlay, key=lambda item: item[1])
        largest = max(per_overlay, key=lambda item: item[1])
        print(
            f"per-overlay range: {smallest[1]} ({smallest[0]}) .. "
            f"{largest[1]} ({largest[0]})"
        )
    print(f"base DTBs scanned: {len(per_base)}")
    if per_base:
        smallest = min(per_base, key=lambda item: item[1])
        largest = max(per_base, key=lambda item: item[1])
        print(
            f"per-base range   : {smallest[1]} ({smallest[0]}) .. "
            f"{largest[1]} ({largest[0]})"
        )
    print(f"symbols exported : {len(symbols)} (union)")
    if renames:
        print(f"nodes renamed    : {['%s -> %s' % r for r in renames]}")
    print(f"sink node        : {SINK_PATH}")
    print(f"sink mode        : {mode}")
    print(f"output           : {args.output}")


if __name__ == "__main__":
    sys.exit(main())
