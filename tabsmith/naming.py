"""Work out a song's title/artist and the 'Title - Artist Part N' naming scheme."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Characters that are illegal in filenames on macOS/Windows, plus control chars.
_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def sanitize(name: str) -> str:
    """Make `name` safe to use as a filename component."""
    cleaned = _ILLEGAL.sub("-", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Untitled"


@dataclass
class SongInfo:
    title: str
    artist: str
    duration: float  # seconds

    @property
    def stem(self) -> str:
        """'Hum - Murtaza Qizilbash' — the part shared by every chunk."""
        if self.artist:
            return sanitize(f"{self.title} - {self.artist}")
        return sanitize(self.title)

    def part_name(self, n: int) -> str:
        """'Hum - Murtaza Qizilbash Part 1'."""
        return f"{self.stem} Part {n}"


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe could not read {path.name}:\n{out.stderr.strip()}")
    return json.loads(out.stdout or "{}")


def _tag(tags: dict, *names: str) -> str:
    """Tag lookup that ignores case (Vorbis uses TITLE, ID3 uses title)."""
    lowered = {k.lower(): v for k, v in tags.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v and str(v).strip():
            return str(v).strip()
    return ""


def _from_filename(path: Path) -> tuple[str, str]:
    """Parse 'Artist - Title.mp3'. Returns (title, artist); artist may be ''."""
    stem = path.stem.strip()
    # Only split on a dash surrounded by whitespace, so 'Hum-Dum' stays intact.
    parts = re.split(r"\s+[-–—]\s+", stem, maxsplit=1)
    if len(parts) == 2:
        left, right = (p.strip() for p in parts)
        if left and right:
            return right, left  # convention on disk is 'Artist - Title'
    return stem, ""


def probe(path: Path, title: str | None = None, artist: str | None = None) -> SongInfo:
    """Read duration and metadata. Explicit title/artist always win."""
    meta = _ffprobe(path)

    if not any(s.get("codec_type") == "audio" for s in meta.get("streams", [])):
        raise RuntimeError(f"{path.name} has no audio stream.")

    fmt = meta.get("format", {})
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise RuntimeError(f"Could not determine the duration of {path.name}.")

    tags = dict(fmt.get("tags") or {})
    for stream in meta.get("streams", []):
        if stream.get("codec_type") == "audio":
            tags.update(stream.get("tags") or {})

    tag_title = _tag(tags, "title")
    tag_artist = _tag(tags, "artist", "album_artist", "performer")

    file_title, file_artist = _from_filename(path)

    return SongInfo(
        title=(title or tag_title or file_title).strip(),
        artist=(artist if artist is not None else (tag_artist or file_artist)).strip(),
        duration=duration,
    )
