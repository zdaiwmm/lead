"""Install the two pinned upstream assets for Linux x86_64 GitHub runners."""
import hashlib
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from camoufox import fpgen_model
from camoufox.browser_pin import load_pin
from camoufox.multiversion import find_installed_version
from camoufox.pkgman import AvailableVersion, CamoufoxFetcher, Version, launch_path

API = "https://api.github.com/repos"
BROWSER_SHA = "72637885ee67c93466ad377831c647192f8f08fb5c6fd9279de1320b49dd2b53"


def download(url, stream):
    subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "4",
         "--retry-all-errors", "--connect-timeout", "15", "--max-time", "360",
         "--header", "Accept: application/octet-stream", url],
        stdout=stream, check=True,
    )
    stream.seek(0)
    return stream


class GitHubAssetFetcher(CamoufoxFetcher):
    @staticmethod
    def download_file(file, url):
        return download(url, file)


def install():
    pin = load_pin()
    if not pin or pin.tag != "v156.0.1-beta.36" or fpgen_model.PIN["tag"] != "model-2/2026":
        raise RuntimeError("Update the CI asset pins to match the locked SDK")
    version = AvailableVersion(
        version=Version(version=pin.version, build=pin.build),
        url=f"{API}/daijro/camoufox/releases/assets/614468056?download=1",
        is_prerelease=True, sha256=BROWSER_SHA,
    )
    # The SDK verifies the browser archive digest before extracting/activating it.
    GitHubAssetFetcher(selected_version=version).install()
    browser = find_installed_version("official/156.0.1-beta.36")
    if not browser or not Path(launch_path(browser)).is_file():
        raise RuntimeError("Expected Camoufox executable was not installed")

    with TemporaryDirectory() as tmp:
        archive = Path(tmp) / "model.zip"
        with archive.open("w+b") as stream:
            download(f"{API}/scrapfly/fingerprint-generator/releases/assets/357981401?download=1", stream)
            if hashlib.file_digest(stream, "sha256").hexdigest() != fpgen_model.PIN["sha256"]:
                raise RuntimeError("Fingerprint model checksum mismatch")
        target = fpgen_model.fpgen_data_dir()
        target.mkdir(parents=True, exist_ok=True)
        with ZipFile(archive) as model:
            for name in fpgen_model.PIN["file_sha256"]:
                (target / name).write_bytes(model.read(name))
        fpgen_model.ensure_fpgen_model(target)


if __name__ == "__main__":
    install()
