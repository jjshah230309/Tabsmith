"""Stitch the per-chunk Guitar Pro files back into one score.

Songsterr transcribes each chunk independently, so every file arrives as its own
little song: its own measure headers, its own tempo, sometimes a different set of
tracks. Merging therefore means three things:

1. concatenating the song-level measure-header list and re-deriving bar numbers
   and tick positions (GP stores an absolute `start` per bar);
2. lining tracks up *across* files, padding with full-bar rests wherever a chunk
   is missing a track another chunk has, so every track keeps the same bar count;
3. baking each chunk's detected tempo in as a mix-table change on its first beat,
   so a chunk the AI heard at 96 BPM still plays at 96 inside the merged score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import guitarpro as gp
from guitarpro import models as m

QUARTER = m.Duration.quarterTime  # 960 ticks


def bar_ticks(sig: m.TimeSignature) -> int:
    """Length of one bar in ticks."""
    return int(sig.numerator * (4 * QUARTER) / sig.denominator.value)


def is_gpif(path: Path) -> bool:
    """GP7/GP8 (.gp) and GPX are zip containers; GP3-5 are flat binaries."""
    with open(path, "rb") as fh:
        return fh.read(2) == b"PK"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


@dataclass
class _Slot:
    """One track of the merged score, accumulating measures across chunks."""
    proto: m.Track
    key: tuple
    name: str
    measures: list = field(default_factory=list)


def _track_keys(tr: m.Track) -> tuple[tuple, tuple]:
    """(strict, loose) identity keys for matching a track across chunks."""
    tuning = tuple(s.value for s in (tr.strings or []))
    loose = (bool(tr.isPercussionTrack), tuning)
    return (loose + (_norm(tr.name),)), loose


def _rest_measure(track: m.Track, header: m.MeasureHeader) -> m.Measure:
    """A bar of silence, so padded tracks stay bar-aligned with the rest."""
    meas = m.Measure(track, header)
    voices = []
    for i in range(2):  # GP5 always carries two voices
        v = m.Voice(meas)
        if i == 0:
            beat = m.Beat(v, duration=m.Duration(value=1))  # whole-bar rest
            beat.status = m.BeatStatus.rest
            v.beats = [beat]
        voices.append(v)
    meas.voices = voices
    return meas


def _set_tempo(measure: m.Measure, tempo: int) -> bool:
    """Attach a tempo change to a bar's first beat. True if it landed."""
    for voice in measure.voices:
        if not voice.beats:
            continue
        beat = voice.beats[0]
        change = beat.effect.mixTableChange or m.MixTableChange()
        change.tempo = m.MixTableItem(value=int(tempo), duration=0, allTracks=True)
        change.hideTempo = False
        beat.effect.mixTableChange = change
        return True
    return False


def merge_gp(
    paths: list[Path],
    out_path: Path,
    title: str = "",
    artist: str = "",
    keep_tempos: bool = True,
    version: tuple | None = (5, 1, 0),
) -> dict:
    """Merge GP3/GP4/GP5 files, in the order given, into `out_path`."""
    if not paths:
        raise ValueError("nothing to merge")

    songs = []
    for p in paths:
        if is_gpif(p):
            raise RuntimeError(
                f"{p.name} is a GP7/GP8 (.gp) container, not GP3-5. "
                "Use merge_gpif() for those."
            )
        with open(p, "rb") as fh:
            songs.append(gp.parse(fh))

    base = songs[0]
    merged_headers: list[m.MeasureHeader] = []
    slots: list[_Slot] = []
    by_strict: dict[tuple, _Slot] = {}
    by_loose: dict[tuple, _Slot] = {}
    report = {"files": len(songs), "bars_per_file": [], "tracks": [], "tempos": []}

    for song in songs:
        chunk_start_index = len(merged_headers)
        n_bars = len(song.measureHeaders)
        report["bars_per_file"].append(n_bars)
        report["tempos"].append(song.tempo)

        merged_headers.extend(song.measureHeaders)

        # Match this chunk's tracks onto existing slots.
        used: set[int] = set()
        pairing: list[tuple[_Slot, m.Track]] = []
        for tr in song.tracks:
            strict, loose = _track_keys(tr)
            slot = by_strict.get(strict) or by_loose.get(loose)
            if slot is not None and id(slot) not in used:
                pairing.append((slot, tr))
                used.add(id(slot))
                continue
            slot = _Slot(proto=tr, key=strict, name=tr.name)
            # Backfill the bars this track missed in earlier chunks.
            slot.measures = [_rest_measure(tr, h) for h in merged_headers[:chunk_start_index]]
            slots.append(slot)
            by_strict.setdefault(strict, slot)
            by_loose.setdefault(loose, slot)
            pairing.append((slot, tr))
            used.add(id(slot))

        matched = {id(s) for s, _ in pairing}
        for slot, tr in pairing:
            measures = list(tr.measures)[:n_bars]
            # A malformed chunk could be short on bars; pad so nothing desyncs.
            measures += [_rest_measure(tr, h) for h in song.measureHeaders[len(measures):]]
            if keep_tempos and measures:
                _set_tempo(measures[0], song.tempo)
            slot.measures.extend(measures)
        for slot in slots:
            if id(slot) not in matched:
                slot.measures.extend(_rest_measure(slot.proto, h) for h in song.measureHeaders)

    # Renumber bars and recompute absolute tick positions.
    start = QUARTER
    prev_sig = None
    for i, h in enumerate(merged_headers, start=1):
        h.number = i
        h.start = start
        # GP omits a repeated signature; make each header self-describing.
        if prev_sig is not None and h.timeSignature is None:
            h.timeSignature = prev_sig
        prev_sig = h.timeSignature
        start += bar_ticks(h.timeSignature)

    out = m.Song(
        title=title or base.title,
        artist=artist or base.artist,
        album=base.album,
        tempo=base.tempo,
        key=base.key,
    )
    out.measureHeaders = merged_headers
    out.tracks = []
    for n, slot in enumerate(slots, start=1):
        tr = slot.proto
        tr.song = out
        tr.number = n
        if not tr.strings:
            # GP5 writing indexes strings[-1]; a stringless track would crash it.
            tr.strings = [m.GuitarString(i + 1, v)
                          for i, v in enumerate((64, 59, 55, 50, 45, 40))]
        tr.measures = slot.measures
        for meas, header in zip(tr.measures, merged_headers):
            meas.track = tr
            meas.header = header
        out.tracks.append(tr)
        report["tracks"].append({"name": tr.name, "bars": len(tr.measures)})

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        gp.write(out, fh, version=version)

    report["total_bars"] = len(merged_headers)
    report["out"] = str(out_path)
    return report
