"""DWG -> DXF conversion via LibreDWG's `dwg2dxf` CLI (installed in the Docker image)."""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from app.config import Settings

logger = logging.getLogger(__name__)

DWG_MAGIC_PREFIX = b"AC10"  # e.g. AC1015 (R2000) ... AC1032 (R2018+)


class ConversionError(Exception):
    """The DWG could not be converted (tool missing, unsupported version, corrupt file, timeout)."""


def detect_format(filename: str, data: bytes) -> str | None:
    """Return 'dwg' or 'dxf' based on file content, or None if it is neither."""
    if data.startswith(DWG_MAGIC_PREFIX):
        return "dwg"
    if data.startswith(b"AutoCAD Binary DXF"):
        return "dxf"
    text_head = data[:2048].removeprefix(b"\xef\xbb\xbf").lstrip().decode("latin-1", errors="ignore").upper()
    if (text_head.startswith("0") and "SECTION" in text_head) or text_head.startswith("999"):
        return "dxf"
    # Unusual-but-valid DXF headers: let the DXF reader decide (it reports a clear error).
    if Path(filename).suffix.lower() == ".dxf" and text_head:
        return "dxf"
    return None


def converter_available(settings: Settings) -> bool:
    return shutil.which(settings.dwg2dxf_path) is not None


async def convert_dwg_to_dxf(data: bytes, settings: Settings) -> tuple[bytes, list[str]]:
    """Convert DWG bytes to DXF bytes. Returns (dxf_bytes, warnings)."""
    binary = shutil.which(settings.dwg2dxf_path)
    if binary is None:
        raise ConversionError(
            f"DWG converter '{settings.dwg2dxf_path}' is not installed. Run the backend via Docker "
            "(LibreDWG is built into the image) or upload a DXF instead."
        )

    with tempfile.TemporaryDirectory(prefix="walkitecture-") as tmp:
        src, dst = Path(tmp) / "input.dwg", Path(tmp) / "output.dxf"
        src.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            binary, "-y", "-o", str(dst), str(src),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=settings.dwg_convert_timeout_s)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ConversionError(f"DWG conversion timed out after {settings.dwg_convert_timeout_s:.0f} s.") from exc

        log_tail = stderr.decode("utf-8", errors="replace").strip()[-800:]
        if not dst.exists() or dst.stat().st_size == 0:
            logger.warning("dwg2dxf failed (exit %s): %s", proc.returncode, log_tail)
            raise ConversionError(
                f"DWG conversion failed (exit code {proc.returncode}). The file may be corrupt or use an "
                f"unsupported DWG version. Converter output: {log_tail or 'none'}"
            )

        warnings = []
        if proc.returncode != 0:
            # LibreDWG reports recoverable issues via non-zero exit codes but still writes output.
            warnings.append(f"DWG converter reported recoverable issues (exit code {proc.returncode}).")
        return dst.read_bytes(), warnings
