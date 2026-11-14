from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

from .constants import MAX_UPDATE, parse_version


def source_version(data: bytes, path: Path) -> str:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ValueError("helper update must be a zipapp archive")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            names = [info.filename for info in members]
            if len(members) > 128 or len(set(names)) != len(names) \
                    or not {"VERSION", "__main__.py", "helper/constants.py"}.issubset(names) \
                    or sum(info.file_size for info in members) > MAX_UPDATE:
                raise ValueError("invalid helper archive")
            for info in members:
                name = info.filename
                if (name not in {"VERSION", "__main__.py"} and not name.startswith("helper/")) \
                        or name != "VERSION" and not name.endswith(".py") \
                        or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")) \
                        or stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("invalid helper archive member")
                if name != "VERSION":
                    compile(archive.read(info), str(path) + "/" + name, "exec")
            return parse_version(archive.read("VERSION"))
    except (zipfile.BadZipFile, RuntimeError):
        raise ValueError("invalid helper archive") from None
