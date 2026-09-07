"""Dedupe scan report / calibration tool.

Usage:
    python -m tools.dedupe_report --dry-run
    python -m tools.dedupe_report --dry-run --phash-max 12 --dhash-max 12

`--dry-run`:
    Runs the full dedupe_scan (which is safe — it only rewrites pending
    groups; resolved and not-duplicates are left alone). Prints the
    group-size counts, min-distance histogram, elapsed time, and renders
    an HTML page of the 50 nearest pairs above the threshold (11..20)
    to %LOCALAPPDATA%\\PhotoArchive\\reports\\ so George can eyeball
    whether the threshold is set right before sitting down to review.
"""
from __future__ import annotations

import argparse
import html
import os
import sys
import time
from pathlib import Path

from photoarchive import db
from photoarchive.config import load as load_config
from photoarchive.modes.ingest.paths import thumb_path
from photoarchive.modes.dedupe import scan as scan_mod
from photoarchive.modes.dedupe.index import (
    MAX_SAFE_DISTANCE, MultiIndex, hamming_int, hex_to_int,
)


def _reports_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        p = Path(base) / "PhotoArchive" / "reports"
    else:
        # Non-Windows fallback (unit tests / dev)
        p = Path.home() / ".local" / "share" / "PhotoArchive" / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _print_stats(stats) -> None:
    d = stats.to_dict() if hasattr(stats, "to_dict") else stats
    print(f"Photos scanned:   {d['photos_scanned']}")
    print(f"Thumbs missing:   {d['thumbs_missing']}")
    print(f"Variant errors:   {d['variant_errors']}")
    print(f"Candidate pairs:  {d['candidate_pairs']}")
    print(f"Groups created:   {d['groups_created']}")
    print(f"Elapsed:          {d['elapsed_seconds']:.2f} s")
    gs = d.get("groups_by_size") or {}
    if gs:
        print("\nGroup size histogram:")
        for size in sorted(gs):
            print(f"  size {size:>3}: {gs[size]}")
    hh = d.get("min_distance_histogram") or {}
    if hh:
        print("\nMin-distance histogram:")
        for dist in sorted(hh):
            print(f"  dist {dist:>3}: {hh[dist]}")


def _near_miss_pairs(
    limit: int = 50, band_low: int = 11, band_high: int = 20,
) -> list[dict]:
    """Return the nearest N pairs whose distance lies just above the
    configured threshold, for calibration. We rebuild a smaller index
    over the identity hashes only (rotated variants are excluded — this
    is a shape check, not a duplicate check).
    """
    settings = load_config()
    with db.connection() as conn:
        # Same back-exclusion as scan._load_keep_photos so the near-miss
        # report is consistent with what a real scan would consider.
        rows = conn.execute("""
            select id, phash, dhash, source_folder, source_filename
            from photos p
            where triage_status in ('keep', 'private')
              and is_deleted = false
              and phash is not null and dhash is not null
              and not exists (
                select 1 from triage_hints h
                where h.photo_id = p.id and h.hint = 'possible_back'
              )
              and not exists (
                select 1 from ingest_pairings ip
                where ip.back_photo_id = p.id
              )
        """).fetchall()

    phash_items: list[tuple[object, int]] = []
    dhash_items: list[tuple[object, int]] = []
    meta: dict[int, dict] = {}
    for pid, ph_hex, dh_hex, folder, filename in rows:
        try:
            ph = hex_to_int(ph_hex)
            dh = hex_to_int(dh_hex)
        except ValueError:
            continue
        phash_items.append((pid, ph))
        dhash_items.append((pid, dh))
        meta[pid] = {
            "id": pid, "source_folder": folder, "source_filename": filename,
        }

    phash_index = MultiIndex.build(phash_items)
    dhash_index = MultiIndex.build(dhash_items)

    pairs: dict[tuple[int, int], dict] = {}

    def _try(index: MultiIndex, items: list[tuple[object, int]], algo: str) -> None:
        for pid, h in items:
            for other, d in index.query(h, band_high):
                # In this tool the index keys are photo ids (not
                # (photo_id, transform) as in the scan orchestrator).
                if other == pid:
                    continue
                if d < band_low:
                    continue
                key = (pid, other) if pid < other else (other, pid)
                entry = pairs.get(key)
                if entry is None or d < entry["distance"]:
                    pairs[key] = {
                        "a": key[0], "b": key[1],
                        "distance": d, "algo": algo,
                    }

    if band_high <= MAX_SAFE_DISTANCE:
        _try(phash_index, phash_items, "phash")
        _try(dhash_index, dhash_items, "dhash")
    else:
        # Fall back to brute force for very large bands.
        print(f"  (band_high {band_high} > safe {MAX_SAFE_DISTANCE}; brute force)")
        for i, (pa, ha) in enumerate(phash_items):
            for pb, hb in phash_items[i + 1:]:
                d = hamming_int(ha, hb)
                if band_low <= d <= band_high:
                    key = (pa, pb) if pa < pb else (pb, pa)
                    entry = pairs.get(key)
                    if entry is None or d < entry["distance"]:
                        pairs[key] = {
                            "a": key[0], "b": key[1],
                            "distance": d, "algo": "phash",
                        }

    top = sorted(pairs.values(), key=lambda p: p["distance"])[:limit]
    for row in top:
        row["a_meta"] = meta.get(row["a"], {"id": row["a"]})
        row["b_meta"] = meta.get(row["b"], {"id": row["b"]})
        row["a_thumb"] = thumb_path(settings, row["a"])
        row["b_thumb"] = thumb_path(settings, row["b"])
    return top


def _write_html(rows: list[dict], out_path: Path) -> None:
    def _thumb_uri(p: Path) -> str:
        return "file:///" + str(p).replace("\\", "/")

    parts: list[str] = []
    parts.append("<!doctype html><meta charset='utf-8'>")
    parts.append("<title>Dedupe near-miss report</title>")
    parts.append(
        "<style>"
        "body{font-family:Consolas,Menlo,monospace;background:#111;color:#eee;padding:8px;}"
        "table{border-collapse:collapse;} "
        "td,th{padding:6px;border:1px solid #333;vertical-align:top;} "
        "img{max-width:260px;max-height:260px;display:block;} "
        "th{background:#222;text-align:left;}"
        "</style>"
    )
    parts.append(f"<h1>Dedupe near-miss report — {len(rows)} pairs</h1>")
    parts.append(
        "<p>Pairs whose closest Hamming distance is just above the "
        "configured threshold. Use this to calibrate DEDUPE_PHASH_MAX / "
        "DEDUPE_DHASH_MAX.</p>"
    )
    parts.append("<table><tr>"
                 "<th>Distance</th><th>Algo</th>"
                 "<th>A</th><th>B</th></tr>")
    for row in rows:
        a = row["a_meta"]
        b = row["b_meta"]
        parts.append(
            f"<tr><td>{row['distance']}</td><td>{html.escape(row['algo'])}</td>"
            f"<td><img src=\"{_thumb_uri(row['a_thumb'])}\">"
            f"<div>#{a['id']} — {html.escape(a.get('source_folder', ''))}/"
            f"{html.escape(a.get('source_filename', ''))}</div></td>"
            f"<td><img src=\"{_thumb_uri(row['b_thumb'])}\">"
            f"<div>#{b['id']} — {html.escape(b.get('source_folder', ''))}/"
            f"{html.escape(b.get('source_filename', ''))}</div></td>"
            f"</tr>"
        )
    parts.append("</table>")
    out_path.write_text("\n".join(parts), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="run scan and print stats + write HTML")
    parser.add_argument("--near-miss-only", action="store_true",
                        help="skip the scan; just render the near-miss HTML")
    parser.add_argument("--phash-max", type=int, default=None,
                        help="override DEDUPE_PHASH_MAX for this run only")
    parser.add_argument("--dhash-max", type=int, default=None,
                        help="override DEDUPE_DHASH_MAX for this run only")
    parser.add_argument("--near-miss-count", type=int, default=50)
    args = parser.parse_args(argv)

    settings = load_config()
    db.init_pool(settings)

    if args.phash_max is not None:
        object.__setattr__(settings, "DEDUPE_PHASH_MAX", args.phash_max)
    if args.dhash_max is not None:
        object.__setattr__(settings, "DEDUPE_DHASH_MAX", args.dhash_max)

    print(f"Dedupe thresholds: pHash <= {settings.DEDUPE_PHASH_MAX}, "
          f"dHash <= {settings.DEDUPE_DHASH_MAX}")

    if not args.dry_run and not args.near_miss_only:
        parser.print_help()
        return 1

    t0 = time.perf_counter()
    if not args.near_miss_only:
        stats = scan_mod.run_dedupe_scan(settings)
        print(f"\n=== dedupe_scan results ===")
        _print_stats(stats)

    print(f"\n=== near-miss pairs (distance "
          f"{max(1, settings.DEDUPE_PHASH_MAX) + 1}..{settings.DEDUPE_PHASH_MAX + 10}) ===")
    band_low = max(settings.DEDUPE_PHASH_MAX, settings.DEDUPE_DHASH_MAX) + 1
    band_high = min(MAX_SAFE_DISTANCE, band_low + 9)
    if band_high < band_low:
        print("  (threshold already at safe upper bound; skipping near-miss render)")
    else:
        near = _near_miss_pairs(
            limit=args.near_miss_count, band_low=band_low, band_high=band_high,
        )
        out_dir = _reports_dir()
        out_path = out_dir / f"dedupe-near-miss-{int(time.time())}.html"
        _write_html(near, out_path)
        print(f"  Rendered {len(near)} pairs to {out_path}")

    print(f"\nTotal wall time: {time.perf_counter() - t0:.2f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
