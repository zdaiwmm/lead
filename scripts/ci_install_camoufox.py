"""Provision the repository's pinned Linux x86_64 native browser in disposable CI."""

import subprocess

from camoufox.multiversion import find_installed_version, install_versioned
from camoufox.pkgman import AvailableVersion, CamoufoxFetcher, RepoConfig, Version

BUILD = "156.0.1-beta.36"
DIGEST = "72637885ee67c93466ad377831c647192f8f08fb5c6fd9279de1320b49dd2b53"
URL = f"https://github.com/daijro/camoufox/releases/download/v{BUILD}/camoufox-{BUILD}-lin.x86_64.zip"


class LinuxReleaseFetcher(CamoufoxFetcher):
    @staticmethod
    def download_file(file, url):
        # Both endpoints identify the same pinned public release asset. The SDK
        # checks the published digest before unzipping any of the bytes.
        asset = "https://api.github.com/repos/daijro/camoufox/releases/assets/614468056"
        for location in (url, asset):
            file.seek(0)
            file.truncate()
            result = subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error",
                                     "--retry", "2", "--retry-all-errors", "--connect-timeout", "10",
                                     "--max-time", "180", "--header", "Accept: application/octet-stream",
                                     "--output", file.name, location], check=False)
            if result.returncode == 0:
                file.seek(0)
                return file
        raise RuntimeError("Pinned browser download unavailable")


if __name__ == "__main__":
    release = AvailableVersion(Version("beta.36", "156.0.1"), URL, is_prerelease=True, sha256=DIGEST)
    fetcher = LinuxReleaseFetcher(repo_config=RepoConfig.find_by_name("official"), selected_version=release)
    install_versioned(fetcher)
    path = find_installed_version("official/" + BUILD)
    if path is None or not (path / "camoufox").is_file():
        raise SystemExit("Pinned native browser missing after installation")
