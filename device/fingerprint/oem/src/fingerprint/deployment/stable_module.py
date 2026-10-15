#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Late load the exact stable FPC module. Never rebind GENI or write boot."""
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys


def readiness_services(candidate):
    """Installed systems must reach the greeter without a debug network."""
    mode = candidate.get("deployment_mode", "development")
    display = candidate.get("display_service", "gdm")
    if display not in ("gdm", "display-manager", "plasmalogin"):
        raise ValueError("Unsupported fingerprint display service")
    desktop = (display, "iio-sensor-proxy")
    if mode == "installed":
        return desktop
    if mode != "development":
        raise ValueError("Unsupported fingerprint deployment mode")
    return (candidate.get("control_service", "ssh"), "NetworkManager", *desktop)


def render_module_unit(template, candidate):
    services = " ".join(name.removesuffix(".service") + ".service"
                        for name in readiness_services(candidate))
    return "\n".join(line.split("=", 1)[0] + "=" + services
                     if line.startswith(("After=", "Wants=")) else line
                     for line in template.splitlines()) + "\n"


def installed_kernel_module(root=Path("/"), release=None):
    """Use the running kernel's authenticated V2 module record, not a copied KO."""
    sys.path.insert(0, str(root / "usr/lib/liuqin"))
    from liuqin_kernel import parse_contract, module_records
    release = release or os.uname().release
    directory = root / "usr/share/liuqin/kernels" / release
    contract = parse_contract((directory / "modules.contract").read_bytes())
    manifest = (directory / "modules.manifest").read_bytes()
    if (contract["release"] != release or
            hashlib.sha256(manifest).hexdigest() != contract["modules_sha256"]):
        raise RuntimeError("Installed FPC kernel manifest mismatch")
    name = "usr/lib/modules/" + release + "/kernel/drivers/misc/fpc1264_spi_diag.ko"
    records = module_records(manifest, release)
    if name not in records:
        raise RuntimeError("The running kernel has no authenticated FPC module")
    module = root / name
    if module.is_symlink() or hashlib.sha256(module.read_bytes()).hexdigest() != records[name]:
        raise RuntimeError("Installed FPC module hash mismatch")
    return module


def load():
    if os.geteuid() != 0:
        raise RuntimeError("root required")
    here = Path(__file__).resolve().parent
    candidate = json.loads((here / "CANDIDATE.json").read_text())
    versioned = candidate.get("kernel_profile") == "liuqin-root-v2"
    if not Path("/dev/tee0").exists() or (not versioned and os.uname().release != candidate["kernel_release"]):
        raise RuntimeError("Start the stable FPC support boot using the host boot entry")
    node = Path("/sys/bus/spi/devices/spi1.0/of_node")
    if not node.exists() or b"fpc,fpc1020" not in (node / "compatible").read_bytes():
        raise RuntimeError("Stable fingerprint device tree is unavailable")
    regulator = Path("/sys/firmware/devicetree/base/soc@0/rsc@17a00000/regulators-1/ldo9")
    if ((node / "vdd-supply").read_bytes() != (regulator / "phandle").read_bytes() or
        int.from_bytes((regulator / "regulator-min-microvolt").read_bytes(), "big") != 2960000 or
        int.from_bytes((regulator / "regulator-max-microvolt").read_bytes(), "big") != 3008000):
        raise RuntimeError("Stable FPC voltage range was not verified")
    for service in readiness_services(candidate):
        if subprocess.run(["systemctl", "is-active", "--quiet", service]).returncode:
            raise RuntimeError("Fingerprint startup dependency is not ready: " + service)
    module = installed_kernel_module() if versioned else here / "kernel/fpc1264_spi_diag.ko"
    if not versioned and hashlib.sha256(module.read_bytes()).hexdigest() != candidate["kernel_module_sha256"]:
        raise RuntimeError("Stable FPC module hash mismatch")
    if candidate.get("firmware_source") == "modem_a-read-only":
        from provision_firmware import provision
        provision(here / "firmware")
    if not Path("/dev/fpc1020").exists():
        subprocess.run(["insmod", str(module)], check=True)
        subprocess.run(["udevadm", "settle", "--timeout=5"], check=True)
    if not Path("/dev/fpc1020").exists():
        raise RuntimeError("FPC module did not create the device")
    if versioned:
        try:
            interface = (node.parent / "irq_interface").read_text().strip()
        except OSError:
            interface = "unavailable"
        if interface != "level-v1":
            raise RuntimeError("FPC kernel requires the level-v1 IRQ interface; install a matching Liuqin kernel")
    print("stable_fpc_module=READY late_load=1 geni_rebind=0 partition_writes=0")

if __name__ == "__main__":
    load()
