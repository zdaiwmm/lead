"""Linux browser launch policy. Only the profile's Unix proxy bridge is mounted."""

import os
import platform
import shutil
from pathlib import Path

from .config import ROOT, STATE, env


def required():
    return (
        env().get("LEAD_DEPLOYMENT_MODE") == "production"
        or env().get("LEAD_NETWORK_NAMESPACE") == "1"
    )


def apply(options, aid, proxy, storage=None):
    if not required():
        return options
    if (
        platform.system() != "Linux"
        or not shutil.which("bwrap")
        or not Path("/usr/bin/python3").is_file()
    ):
        raise ValueError("此浏览器节点不能提供系统级出口隔离")
    wrapper = ROOT / "scripts/browser_namespace.py"
    if not os.access(wrapper, os.X_OK):
        raise ValueError("浏览器隔离启动程序未安装")
    # SDK validation and fingerprint configuration run against the real binary;
    # only the final executable is replaced, preserving native Juggler pipes.
    return {
        **options,
        "executable_path": str(wrapper),
        "env": {
            **options["env"],
            "LEAD_BOUNDARY_NONCE": proxy.boundary_nonce,
            "LEAD_REAL_BROWSER": str(Path(options["executable_path"]).resolve()),
            "LEAD_BROWSER_PROFILE": str(storage or STATE / "profiles" / aid / "storage"),
            "LEAD_BROWSER_ASSETS": str(STATE / "assets" / aid),
            "LEAD_BROWSER_SOCKET": str(proxy.socket_path),
            "LEAD_BROWSER_PROXY_PORT": str(proxy.port),
        },
    }


def chromium_options(options, aid, proxy, storage, *, control_directory, cdp_port):
    """Use the existing namespace with an explicit private reverse CDP bridge.

    The instance manager owns and removes control_directory and consumes its
    cdp.sock. This function never creates a host TCP listener or SDK endpoint.
    """
    if not required():
        raise ValueError("Chromium control bridge requires the Linux network boundary")
    directory = Path(control_directory)
    if (not directory.is_absolute() or directory.resolve(strict=True) != directory
            or not directory.is_dir() or directory.stat().st_uid != os.getuid()
            or directory.stat().st_mode & 0o777 != 0o700 or any(directory.iterdir())):
        raise ValueError("A fresh manager-owned private control directory is required")
    port = int(cdp_port)
    if not 1024 <= port <= 65535 or port == proxy.port:
        raise ValueError("Invalid assigned CDP port")
    args = options.get("args", [])
    for name, value in (("--remote-debugging-port", str(port)),
                        ("--remote-debugging-address", "127.0.0.1")):
        if [arg for arg in args if arg == name or arg.startswith(name + "=")] != [name + "=" + value]:
            raise ValueError("Chromium arguments differ from the assigned control endpoint")
    settings = apply(options, aid, proxy, storage)
    settings["env"].update({
        "LEAD_BROWSER_CONTROL_DIRECTORY": str(directory),
        "LEAD_BROWSER_CDP_PORT": str(port),
    })
    return settings
