from __future__ import annotations

from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
import zipfile


NAME = "aspria_booker"
PROJECT_NAME = "aspria-booker"
VERSION = "0.1.0"
DIST_INFO = f"{NAME}-{VERSION}.dist-info"


def get_requires_for_build_wheel(config_settings=None):  # noqa: ANN001
    return []


def get_requires_for_build_editable(config_settings=None):  # noqa: ANN001
    return []


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):  # noqa: ANN001
    return _write_metadata(Path(metadata_directory))


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):  # noqa: ANN001
    return _write_metadata(Path(metadata_directory))


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):  # noqa: ANN001
    wheel_name = _wheel_name()
    wheel_path = Path(wheel_directory) / wheel_name
    records: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in (Path.cwd() / "src" / NAME).rglob("*.py"):
            arcname = source.relative_to(Path.cwd() / "src").as_posix()
            data = source.read_bytes()
            archive.writestr(arcname, data)
            records.append((arcname, data))
        _write_dist_info_to_wheel(archive, records)
    return wheel_name


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):  # noqa: ANN001
    wheel_name = _wheel_name()
    wheel_path = Path(wheel_directory) / wheel_name
    records: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        pth_name = f"{NAME}.pth"
        data = f"{Path.cwd() / 'src'}\n".encode()
        archive.writestr(pth_name, data)
        records.append((pth_name, data))
        _write_dist_info_to_wheel(archive, records)
    return wheel_name


def _wheel_name() -> str:
    return f"{NAME}-{VERSION}-py3-none-any.whl"


def _write_metadata(directory: Path) -> str:
    dist_info = directory / DIST_INFO
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(_metadata(), encoding="utf-8")
    (dist_info / "WHEEL").write_text(_wheel(), encoding="utf-8")
    (dist_info / "entry_points.txt").write_text(_entry_points(), encoding="utf-8")
    return DIST_INFO


def _write_dist_info_to_wheel(
    archive: zipfile.ZipFile, records: list[tuple[str, bytes]]
) -> None:
    for arcname, data in [
        (f"{DIST_INFO}/METADATA", _metadata().encode()),
        (f"{DIST_INFO}/WHEEL", _wheel().encode()),
        (f"{DIST_INFO}/entry_points.txt", _entry_points().encode()),
    ]:
        archive.writestr(arcname, data)
        records.append((arcname, data))

    record_lines = [
        f"{arcname},sha256={_digest(data)},{len(data)}" for arcname, data in records
    ]
    record_lines.append(f"{DIST_INFO}/RECORD,,")
    archive.writestr(f"{DIST_INFO}/RECORD", "\n".join(record_lines) + "\n")


def _metadata() -> str:
    return (
        "Metadata-Version: 2.3\n"
        f"Name: {PROJECT_NAME}\n"
        f"Version: {VERSION}\n"
        "Requires-Python: >=3.12\n"
        "Summary: Private Aspria Hannover Maschsee course booking CLI\n"
    )


def _wheel() -> str:
    return "Wheel-Version: 1.0\nGenerator: aspria-build\nRoot-Is-Purelib: true\nTag: py3-none-any\n"


def _entry_points() -> str:
    return "[console_scripts]\naspria-booker = aspria_booker.cli:main\n"


def _digest(data: bytes) -> str:
    return urlsafe_b64encode(sha256(data).digest()).rstrip(b"=").decode()
