"""Cut a song into fixed-length, non-overlapping chunks with ffmpeg."""
from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

from .naming import SongInfo, probe

# Encoder settings per output container. Re-encoding (never `-c copy`) is what
# guarantees the cut lands exactly on the requested second: stream copy would
# snap each cut to the nearest keyframe, which both drifts and can overlap.
_ENCODERS = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
    "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
    "m4a": ["-c:a", "aac", "-b:a", "256k"],
}


@dataclass
class Chunk:
    index: int          # 1-based, matches "Part N"
    name: str           # 'Hum - Murtaza Qizilbash Part 1'
    path: str
    start: float        # seconds into the original
    end: float
    duration: float


def plan_chunks(total: float, length: float, min_tail: float = 0.0) -> list[tuple[float, float]]:
    """Non-overlapping [start, end) windows covering `total` seconds.

    Consecutive windows share an edge exactly, so no audio is duplicated and
    none is dropped. If the final window would be shorter than `min_tail`, it is
    absorbed into the previous one (that window then exceeds `length`).
    """
    if length <= 0:
        raise ValueError("chunk length must be positive")

    count = max(1, math.ceil(round(total / length, 6)))
    spans = []
    for i in range(count):
        start = i * length
        spans.append((start, min(start + length, total)))

    if min_tail > 0 and len(spans) > 1:
        last_start, last_end = spans[-1]
        if last_end - last_start < min_tail:
            prev_start, _ = spans[-2]
            spans[-2:] = [(prev_start, last_end)]
    return spans


def split(
    source: Path,
    out_dir: Path,
    length: float = 20.0,
    fmt: str = "mp3",
    title: str | None = None,
    artist: str | None = None,
    min_tail: float = 0.0,
    info: SongInfo | None = None,
) -> tuple[SongInfo, list[Chunk]]:
    """Split `source` into `length`-second parts named '<Title> - <Artist> Part N'."""
    if fmt not in _ENCODERS:
        raise ValueError(f"unsupported format {fmt!r}; choose from {', '.join(_ENCODERS)}")

    info = info or probe(source, title, artist)
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks: list[Chunk] = []
    for i, (start, end) in enumerate(plan_chunks(info.duration, length, min_tail), start=1):
        name = info.part_name(i)
        dest = out_dir / f"{name}.{fmt}"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            # -ss before -i seeks fast; -accurate_seek keeps it sample-exact.
            "-accurate_seek", "-ss", f"{start:.6f}",
            "-i", str(source),
            "-t", f"{end - start:.6f}",
            "-map", "0:a:0", "-vn", "-map_metadata", "-1",
            *_ENCODERS[fmt],
            "-metadata", f"title={name}",
            "-metadata", f"artist={info.artist}",
            str(dest),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg failed on part {i}:\n{res.stderr.strip()}")

        chunks.append(Chunk(
            index=i, name=name, path=str(dest),
            start=round(start, 3), end=round(end, 3),
            duration=round(end - start, 3),
        ))

    manifest = out_dir / "manifest.json"
    manifest.write_text(json.dumps({
        "source": str(source),
        "title": info.title,
        "artist": info.artist,
        "duration": info.duration,
        "chunk_length": length,
        "format": fmt,
        # Ordering lives here, not in filename sort: 'Part 10' sorts before
        # 'Part 2' lexically, and the requested naming has no zero padding.
        "chunks": [asdict(c) for c in chunks],
    }, indent=2))
    return info, chunks


def load_manifest(out_dir: Path) -> tuple[SongInfo, list[Chunk]]:
    data = json.loads((out_dir / "manifest.json").read_text())
    info = SongInfo(data["title"], data["artist"], data["duration"])
    return info, [Chunk(**c) for c in data["chunks"]]
