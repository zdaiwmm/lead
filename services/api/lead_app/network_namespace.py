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


def apply(options, aid, proxy):
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
            "LEAD_REAL_BROWSER": str(Path(options["executable_path"]).resolve()),
            "LEAD_BROWSER_PROFILE": str(STATE / "profiles" / aid / "storage"),
            "LEAD_BROWSER_ASSETS": str(STATE / "assets" / aid),
            "LEAD_BROWSER_SOCKET": str(proxy.socket_path),
            "LEAD_BROWSER_PROXY_PORT": str(proxy.port),
        },
    }
