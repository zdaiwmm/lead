"""Install the pinned browser and fingerprint model in a fresh Linux x86_64 CI environment."""

import hashlib
import subprocess
import tempfile
import time
import zipfile

from camoufox import fpgen_model
from camoufox.multiversion import find_installed_version, install_versioned
from camoufox.pkgman import AvailableVersion, CamoufoxFetcher, RepoConfig, Version

BUILD = "156.0.1-beta.36"
DIGEST = "72637885ee67c93466ad377831c647192f8f08fb5c6fd9279de1320b49dd2b53"
URL = f"https://github.com/daijro/camoufox/releases/download/v{BUILD}/camoufox-{BUILD}-lin.x86_64.zip"


def download(file, url, repo, asset_id, digest):
    api = f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}?ci={time.time_ns()}"
    for location in (url, api):
        file.seek(0)
        file.truncate()
        result = subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error",
                                 "--retry", "2", "--retry-all-errors", "--connect-timeout", "10",
                                 "--max-time", "180", "--header", "Accept: application/octet-stream",
                                 "--output", file.name, location], check=False)
        file.seek(0)
        if result.returncode == 0 and hashlib.file_digest(file, "sha256").hexdigest() == digest:
            file.seek(0)
            return file
    raise RuntimeError("Verified release asset unavailable")


class LinuxReleaseFetcher(CamoufoxFetcher):
    @staticmethod
    def download_file(file, url):
        return download(file, url, "daijro/camoufox", 614468056, DIGEST)


def install_model():
    pin = fpgen_model.PIN
    folder = fpgen_model.fpgen_data_dir()
    folder.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile() as tmp:
        download(tmp, pin["url"], pin["repo"], 357981401, pin["sha256"])
        with zipfile.ZipFile(tmp) as archive:
            for name, digest in pin["file_sha256"].items():
                data = archive.read(name)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError("Fingerprint model member digest mismatch")
                (folder / name).write_bytes(data)
    fpgen_model.ensure_fpgen_model()


if __name__ == "__main__":
    release = AvailableVersion(Version("beta.36", "156.0.1"), URL, is_prerelease=True, sha256=DIGEST)
    fetcher = LinuxReleaseFetcher(repo_config=RepoConfig.find_by_name("official"), selected_version=release)
    install_versioned(fetcher)
    path = find_installed_version("official/" + BUILD)
    if path is None or not (path / "camoufox").is_file():
        raise SystemExit("Pinned native browser missing after installation")
    install_model()
