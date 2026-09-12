from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .merge import merge_gp, is_gpif
from .naming import probe
from .split import split, load_manifest

AUDIO_EXT = {".mp3", ".m4a", ".wav", ".opus", ".flac", ".aac", ".ogg", ".aiff", ".aif", ".wma", ".mp4"}


def _say(*a):
    print(*a, flush=True)


def _work_dir(source: Path, out: str | None) -> Path:
    return Path(out).expanduser() if out else source.expanduser().resolve().parent / f"{source.stem} [tabsmith]"


class State:
    """Records what already succeeded, so a re-run never re-spends credits."""

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {"parts": {}}

    def get(self, name: str) -> dict | None:
        return self.data["parts"].get(name)

    def put(self, name: str, **kw):
        self.data["parts"].setdefault(name, {}).update(kw)
        self.path.write_text(json.dumps(self.data, indent=2))


# ---------------------------------------------------------------- commands
def cmd_split(args) -> int:
    src = Path(args.file).expanduser()
    info = probe(src, args.title, args.artist)
    work = _work_dir(src, args.out)
    parts_dir = work / "parts"
    _say(f"{info.title} — {info.artist or 'unknown artist'}  ({info.duration:.1f}s)")
    info, chunks = split(src, parts_dir, args.seconds, args.format,
                         args.title, args.artist, args.min_tail, info=info)
    for c in chunks:
        _say(f"  [{c.index:>2}] {c.name}.{args.format}   {c.start:.0f}–{c.end:.0f}s")
    _say(f"\n{len(chunks)} parts in {parts_dir}")
    return 0


def cmd_merge(args) -> int:
    files = [Path(p).expanduser() for p in args.files]
    if len(files) == 1 and files[0].is_dir():
        d = files[0]
        manifest = d / "manifest.json"
        found = sorted(p for p in d.rglob("*") if p.suffix.lower() in {".gp5", ".gp4", ".gp3", ".gp"})
        if manifest.exists():
            # Trust the manifest's order; 'Part 10' sorts before 'Part 2' otherwise.
            _, chunks = load_manifest(d)
            order = {c.name: c.index for c in chunks}
            found.sort(key=lambda p: order.get(p.stem, 10**6))
        files = found
    if not files:
        _say("No Guitar Pro files found to merge.")
        return 1
    gpif = [f for f in files if is_gpif(f)]
    if gpif:
        _say("These are GP7/GP8 (.gp) containers, which this merger cannot combine:")
        for f in gpif:
            _say(f"   {f.name}")
        _say("\nRe-export them as Guitar Pro 5 (.gp5), or open them and paste the bars together.")
        return 1
    _say(f"Merging {len(files)} files:")
    for f in files:
        _say(f"   {f.name}")
    out = Path(args.out).expanduser() if args.out else files[0].parent / "merged.gp5"
    rep = merge_gp(files, out, args.title or "", args.artist or "",
                   keep_tempos=not args.flatten_tempo)
    _say(f"\n{rep['total_bars']} bars, {len(rep['tracks'])} tracks -> {rep['out']}")
    for t in rep["tracks"]:
        _say(f"   {t['name']}: {t['bars']} bars")
    return 0


def cmd_login(args) -> int:
    from .songsterr import Songsterr
    with Songsterr(headless=False, log=_say) as s:
        if s.is_signed_in():
            _say("Already signed in.")
            return 0
        return 0 if s.login() else 1


def cmd_quota(args) -> int:
    from .songsterr import Songsterr
    with Songsterr(headless=args.headless, log=_say) as s:
        if not s.is_signed_in():
            _say("Not signed in. Run:  tabsmith login")
            return 1
        q = s.quota()
        _say(f"Transcriptions left this month: {q}" if q is not None
             else "Could not read the quota (endpoint may have changed).")
    return 0


def cmd_run(args) -> int:
    from .songsterr import Songsterr, SongsterrError

    src = Path(args.file).expanduser()
    if not src.exists():
        _say(f"No such file: {src}")
        return 1
    info = probe(src, args.title, args.artist)
    work = _work_dir(src, args.out)
    parts_dir, tabs_dir = work / "parts", work / "tabs"
    work.mkdir(parents=True, exist_ok=True)

    _say(f"{info.title} — {info.artist or 'unknown artist'}  ({info.duration/60:.1f} min)")

    if args.whole:
        chunks, uploads = [], [(1, info.title, src)]
        _say("Whole-song mode: one upload, one transcription credit.")
    else:
        info, chunks = split(src, parts_dir, args.seconds, args.format,
                             args.title, args.artist, args.min_tail, info=info)
        uploads = [(c.index, c.name, Path(c.path)) for c in chunks]
        _say(f"Split into {len(chunks)} × {args.seconds:g}s parts -> {parts_dir}")

    state = State(work / "state.json")
    todo = [u for u in uploads if not (state.get(u[1]) or {}).get("gp")]
    done = len(uploads) - len(todo)
    if done:
        _say(f"{done} part(s) already transcribed — skipping those.")
    if not todo:
        _say("All parts already have tabs; merging.")
        return _merge_downloaded(uploads, state, work, info, args)

    with Songsterr(headless=args.headless, log=_say) as s:
        if not s.is_signed_in():
            _say("\nNot signed in to Songsterr. Run:  tabsmith login")
            return 1
        left = s.quota()
        if left is not None:
            _say(f"Transcriptions left this month: {left}")
            if left < len(todo):
                _say(f"This run needs {len(todo)} but only {left} remain.")
                if not args.yes and input("Continue anyway? [y/N] ").strip().lower() != "y":
                    return 1
        if not args.yes:
            _say(f"\nThis will spend {len(todo)} of your monthly transcriptions.")
            if input("Proceed? [y/N] ").strip().lower() != "y":
                _say("Stopped. Nothing was uploaded.")
                return 1

        for n, (idx, name, path) in enumerate(todo, start=1):
            _say(f"\n[{n}/{len(todo)}] {name}")
            try:
                rec = state.get(name) or {}
                if rec.get("url"):
                    url = rec["url"]
                    _say("    already transcribed; downloading")
                else:
                    t = s.transcribe(path, title=name, artist=info.artist,
                                     tempo=args.tempo, signature=_sig(args.time_signature),
                                     wait_minutes=args.wait)
                    url = t.url
                    state.put(name, url=url, song_id=t.song_id)
                    _say(f"    tab: {url}")
                gp = s.download_gp(url, tabs_dir / name)
                state.put(name, gp=str(gp))
                _say(f"    saved {gp.name}")
            except SongsterrError as e:
                _say(f"    FAILED: {e}")
                if not args.keep_going:
                    _say("\nStopping. Re-run the same command to resume; "
                         "finished parts will not be charged again.")
                    return 1

    return _merge_downloaded(uploads, state, work, info, args)


def _sig(text: str | None):
    if not text:
        return None
    try:
        n, d = text.split("/")
        return int(n), int(d)
    except ValueError:
        raise SystemExit(f"--time-signature wants something like 4/4, got {text!r}")


def _merge_downloaded(uploads, state: State, work: Path, info, args) -> int:
    files, missing = [], []
    for _, name, _p in uploads:
        rec = state.get(name) or {}
        if rec.get("gp") and Path(rec["gp"]).exists():
            files.append(Path(rec["gp"]))
        else:
            missing.append(name)
    if missing:
        _say(f"\n{len(missing)} part(s) have no tab; they will be left out:")
        for m_ in missing:
            _say(f"   {m_}")
    if not files:
        _say("Nothing to merge.")
        return 1
    if any(is_gpif(f) for f in files):
        _say("\nSongsterr returned GP7/GP8 (.gp) files, which cannot be merged here.")
        _say(f"The individual tabs are in {work / 'tabs'}")
        return 1
    out = work / f"{info.stem}.gp5"
    rep = merge_gp(files, out, info.title, info.artist, keep_tempos=not args.flatten_tempo)
    _say(f"\nMerged {rep['files']} tabs -> {out}")
    _say(f"   {rep['total_bars']} bars, {len(rep['tracks'])} tracks")
    for t in rep["tracks"]:
        _say(f"   {t['name']}: {t['bars']} bars")
    return 0


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tabsmith",
        description="Split a song, transcribe the parts with Songsterr's AI, merge the tabs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def audio_opts(sp):
        sp.add_argument("file")
        sp.add_argument("-o", "--out", help="working directory")
        sp.add_argument("-s", "--seconds", type=float, default=20.0, help="chunk length (default 20)")
        sp.add_argument("-f", "--format", default="mp3", choices=["mp3", "m4a", "wav"],
                        help="Songsterr accepts only these three")
        sp.add_argument("--title")
        sp.add_argument("--artist")
        sp.add_argument("--min-tail", type=float, default=0.0,
                        help="fold a final part shorter than this into the previous one")

    sp = sub.add_parser("split", help="only split the audio")
    audio_opts(sp)
    sp.set_defaults(func=cmd_split)

    sp = sub.add_parser("run", help="split, transcribe on Songsterr, download, merge")
    audio_opts(sp)
    sp.add_argument("--whole", action="store_true",
                    help="upload the whole song as ONE transcription instead of chunking")
    sp.add_argument("--tempo", type=int, help="pin every chunk to this BPM")
    sp.add_argument("--time-signature", help="pin every chunk, e.g. 4/4")
    sp.add_argument("--wait", type=float, default=20.0, help="minutes to wait per part")
    sp.add_argument("--headless", action="store_true")
    sp.add_argument("--keep-going", action="store_true", help="continue past a failed part")
    sp.add_argument("--flatten-tempo", action="store_true",
                    help="do not write per-chunk tempo changes into the merged file")
    sp.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("merge", help="merge Guitar Pro files (or a tabsmith folder)")
    sp.add_argument("files", nargs="+")
    sp.add_argument("-o", "--out")
    sp.add_argument("--title")
    sp.add_argument("--artist")
    sp.add_argument("--flatten-tempo", action="store_true")
    sp.set_defaults(func=cmd_merge)

    sp = sub.add_parser("login", help="sign in to Songsterr once, in a real browser")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("quota", help="how many AI transcriptions are left this month")
    sp.add_argument("--headless", action="store_true")
    sp.set_defaults(func=cmd_quota)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _say("\nInterrupted.")
        return 130
    except (RuntimeError, ValueError) as e:
        _say(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
