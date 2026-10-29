"""Measure cold/warm planning with synthetic metadata, never a real account."""

import argparse
import gc
import json
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ibfetch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracks", type=int, default=290584)
    parser.add_argument("--baseline", help="local Git revision to measure instead")
    args = parser.parse_args()
    module = ibfetch
    if args.baseline:
        source = subprocess.check_output(["git", "show", args.baseline + ":ibfetch.py"], cwd=ROOT)
        module = types.ModuleType("benchmark_baseline")
        exec(compile(source, "baseline/ibfetch.py", "exec"), module.__dict__)
    albums, tracks = {}, {}
    for i in range(1, args.tracks + 1):
        album = 1 if i <= 73 else 2 + (i - 74) // 12
        albums.setdefault(str(album), {"artist_id": 1, "name": "Album " + str(album), "year": 1998})
        tracks[str(i)] = {"album_id": album, "artist_id": 1, "title": "Track " + str(i),
                          "track": i, "type": "audio/flac", "file": "/orig/test"}
    lib = module.Library({"artists": {"1": {"name": "Synthetic Artist"}},
                          "albums": albums, "tracks": tracks}, "synthetic", True)
    del albums, tracks
    gc.collect()
    def peak():
        try:
            import resource
            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return value / (1024 ** 2 if sys.platform == "darwin" else 1024)
        except ImportError:
            return None
    before = peak()
    groups = [(1, list(range(1, min(73, args.tracks) + 1)))]
    layout = module.Layout(module.DEFAULT_LAYOUT)
    start = time.perf_counter()
    first = module.plan_jobs(lib, groups, "orig", layout)
    cold = time.perf_counter() - start
    start = time.perf_counter()
    second = module.plan_jobs(lib, groups, "orig", layout)
    warm = time.perf_counter() - start
    assert first == second
    after = peak()
    print(json.dumps({"revision": args.baseline or module.VERSION, "tracks": args.tracks,
                      "selected": len(first), "cold_seconds": round(cold, 4),
                      "warm_seconds": round(warm, 4),
                      "extra_peak_mb": round(after - before, 1) if before is not None else None}))


if __name__ == "__main__":
    main()
