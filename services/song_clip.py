"""cut a short audio snippet from a song's mp3 using ffmpeg (must be on PATH)."""

import asyncio
import os
import shutil
import tempfile

HAS_FFMPEG = shutil.which("ffmpeg") is not None


async def clip(mp3: bytes, start: float, duration: float) -> bytes | None:
    if not HAS_FFMPEG:
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    try:
        tmp.write(mp3)
        tmp.close()
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-v", "error",
            "-ss", f"{start:.2f}",
            "-t", f"{duration:.2f}",
            "-i", tmp.name,
            "-f", "mp3",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return out or None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
