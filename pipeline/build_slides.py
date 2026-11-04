"""Build Deep Zoom (DZI) pyramids for each patient's H&E slides, plus the AI TIL overlay.

For each patient:
  imaging/slides/<id>/slide.dzi, slide_files/<level>/<col>_<row>.jpg, thumbnail.jpg   diagnostic (FFPE) slide
  imaging/slides/<id>/til.png      lymphocyte-infiltration probability (Stony Brook Inception-V4,
                                   Saltz et al. 2018) as a transparent heat map, when IDC has one
  imaging/slides/<id>/label.json   slide metadata, hotspots and the TIL overlay placement
  imaging/slides/<id>/<section>/   with --tissue: the same for each frozen section (e.g. 01a-01-ts1), no TIL

The TIL map is a 50 um grid over the full slide. Its placement is stored in OpenSeadragon viewport
units (fractions of the DZI width), accounting for the bounds the DZI is cropped to.

Usage: python pipeline/build_slides.py [--only TCGA-XX-XXXX] [--workers 8] [--tissue]
"""
from __future__ import annotations

import argparse
import glob
import json
import multiprocessing as mp
import os
import pathlib
import time

import numpy as np
import openslide
import pydicom
from openslide.deepzoom import DeepZoomGenerator
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "work" / "raw"
OUT = ROOT / "work" / "lakehouse" / "imaging"
TILE_SIZE = 1022  # + 2 * overlap = 1024 px tiles
OVERLAP = 1
QUALITY = 82

_dz: DeepZoomGenerator | None = None


def _init(path: str) -> None:
    global _dz
    _dz = DeepZoomGenerator(openslide.OpenSlide(path), TILE_SIZE, OVERLAP, limit_bounds=True)


def _write(job: tuple[int, int, int, str]) -> None:
    level, col, row, dest = job
    if os.path.exists(dest):
        return
    tile = _dz.get_tile(level, (col, row)).convert("RGB")
    tile.save(dest, "JPEG", quality=QUALITY, optimize=True, progressive=True)


def whiten(image: Image.Image) -> Image.Image:
    """Turn the slide's glass background pure white so thumbnails sit cleanly on the page."""
    rgb = np.asarray(image, dtype=np.float32) / 255.0
    mx, mn = rgb.max(axis=2), rgb.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1e-6)
    glass = (sat < 0.08) & (mx > 0.7)
    white = np.median(rgb[glass], axis=0) if glass.any() else np.ones(3)
    out = np.clip(rgb / np.maximum(white, 1e-3), 0, 1)
    omx = out.max(axis=2)
    lift = np.clip((omx - 0.88) / 0.08, 0, 1) * (sat < 0.12)
    out = out + (1 - out) * lift[..., None]
    return Image.fromarray((out * 255).round().astype(np.uint8))


def top_points(score: np.ndarray, count: int, win: int, min_dist: float) -> list[tuple[float, float]]:
    h, w = score.shape
    integral = np.pad(score.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    dens = (integral[win:, win:] - integral[:-win, win:] - integral[win:, :-win] + integral[:-win, :-win]) / win**2
    points: list[tuple[float, float]] = []
    for idx in np.argsort(dens, axis=None)[::-1]:
        y, x = np.unravel_index(idx, dens.shape)
        cy, cx = y + win / 2, x + win / 2
        if dens[y, x] <= 0 or any((cx - p[0]) ** 2 + (cy - p[1]) ** 2 < min_dist**2 for p in points):
            continue
        points.append((cx, cy))
        if len(points) == count:
            break
    return points


def cell_hotspots(thumb: Image.Image, count: int = 4) -> list[dict]:
    rgb = np.asarray(thumb, dtype=np.float32) / 255.0
    h, w, _ = rgb.shape
    mx, mn = rgb.max(axis=2), rgb.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    score = sat * (1.0 - mx) * (rgb[..., 2] >= rgb[..., 0] * 0.8)
    score[(mx > 0.9) | (sat < 0.08)] = 0
    pts = top_points(score, count, max(8, int(min(h, w) * 0.06)), max(h, w) * 0.15)
    return [{"x": round(px / w, 5), "y": round(py / w, 5), "kind": "cellular"} for px, py in pts]


def til_overlay(pid: str, sdir: pathlib.Path, mpp: float, bounds: tuple[int, int], dzi_w: int, dzi_h: int) -> dict | None:
    """Colour the fractional TIL map and place it over the DZI."""
    files = glob.glob(str(RAW / "til" / pid / "*.dcm"))
    frac = next((pydicom.dcmread(f) for f in files if "Fractional" in str(pydicom.dcmread(f, stop_before_pixels=True).SeriesDescription)), None)
    binary = next((pydicom.dcmread(f) for f in files if "Inception-V4 Binary" in str(pydicom.dcmread(f, stop_before_pixels=True).SeriesDescription)), None)
    if frac is None:
        return None
    prob = frac.pixel_array.astype(np.float32)
    if prob.ndim == 3:
        prob = prob[0]
    prob /= float(getattr(frac, "MaximumFractionalValue", 255) or 255)
    spacing_mm = float(frac.SharedFunctionalGroupsSequence[0].PixelMeasuresSequence[0].PixelSpacing[0])
    px_per_cell = spacing_mm * 1000.0 / mpp  # slide level-0 pixels per map cell

    # Teal-to-yellow, alpha rising with probability; cells below 0.4 stay clear so the H&E reads through.
    a = np.clip((prob - 0.4) / 0.45, 0, 1)
    lo, hi = np.array([20, 160, 170]), np.array([250, 225, 60])
    rgb = (lo[None, None] * (1 - a[..., None]) + hi[None, None] * a[..., None]).astype(np.uint8)
    rgba = np.dstack([rgb, (a * 205).astype(np.uint8)])
    Image.fromarray(rgba, "RGBA").save(sdir / "til.png", optimize=True)

    tissue = prob > 0  # cells the classifier scored as tissue
    positive = None
    if binary is not None:
        b = binary.pixel_array
        b = b[0] if b.ndim == 3 else b
        positive = float((b > 0).sum() / max(1, tissue.sum()))
    cols, rows = prob.shape[1], prob.shape[0]
    pts = top_points(np.where(prob > 0.5, prob, 0), 3, 6, max(rows, cols) * 0.12)
    to_vp = lambda cx, cy: {
        "x": round((cx * px_per_cell - bounds[0]) / dzi_w, 5),
        "y": round((cy * px_per_cell - bounds[1]) / dzi_w, 5),
        "kind": "lymphocytes",
    }
    return {
        "url": "til.png",
        "x": round(-bounds[0] / dzi_w, 6),
        "y": round(-bounds[1] / dzi_w, 6),
        "width": round(cols * px_per_cell / dzi_w, 6),
        "gridUm": round(spacing_mm * 1000, 1),
        "cells": int(tissue.sum()),
        "tilPositiveFraction": positive,
        "meanProbability": round(float(prob[tissue].mean()), 4) if tissue.any() else None,
        "hotspots": [to_vp(*p) for p in pts],
        "algorithm": str(frac.SeriesDescription),
        "source": "Saltz J, et al. Spatial organization and molecular correlation of tumor-infiltrating lymphocytes using deep learning on pathology images. Cell Rep. 2018. Converted to DICOM SEG by NCI Imaging Data Commons.",
        "coversHeightFraction": round(rows * px_per_cell / dzi_h, 4),
    }


def process(pid: str, slide: dict, workers: int, section: str | None = None) -> dict:
    """Tile one slide. The diagnostic slide goes to slides/<id>/; a frozen section to slides/<id>/<section>/."""
    src = RAW / "slides" / slide["file_name"]
    base = pid.lower()
    sid = f"{base}/{section}" if section else base
    osr = openslide.OpenSlide(str(src))
    dz = DeepZoomGenerator(osr, TILE_SIZE, OVERLAP, limit_bounds=True)
    sdir = OUT / "slides" / sid
    files = sdir / "slide_files"
    jobs = []
    for level in range(dz.level_count):
        (files / str(level)).mkdir(parents=True, exist_ok=True)
        cols, rows = dz.level_tiles[level]
        jobs += [(level, c, r, str(files / str(level) / f"{c}_{r}.jpg")) for c in range(cols) for r in range(rows)]

    t0 = time.time()
    with mp.Pool(workers, initializer=_init, initargs=(str(src),)) as pool:
        pool.map(_write, jobs, chunksize=32)

    (sdir / "slide.dzi").write_text(dz.get_dzi("jpg"))
    thumb_level = max(i for i, t in enumerate(dz.level_tiles) if t == (1, 1))
    thumb = whiten(dz.get_tile(thumb_level, (0, 0)).convert("RGB"))
    thumb.save(sdir / "thumbnail.jpg", "JPEG", quality=88)

    props = osr.properties
    width, height = dz.level_dimensions[-1]
    mpp = float(props.get(openslide.PROPERTY_NAME_MPP_X) or 0.25)
    bounds = (int(props.get(openslide.PROPERTY_NAME_BOUNDS_X, 0)), int(props.get(openslide.PROPERTY_NAME_BOUNDS_Y, 0)))
    til = None if section else til_overlay(pid, sdir, mpp, bounds, width, height)
    entry = {
        "patientId": pid,
        "slideId": sid,
        "barcode": slide["file_name"].split(".")[0],
        "sourceFile": slide["file_name"],
        "gdcFileId": slide["file_id"],
        "sourceUrl": f"https://portal.gdc.cancer.gov/files/{slide['file_id']}",
        "vendor": props.get(openslide.PROPERTY_NAME_VENDOR),
        "objectivePower": float(props.get(openslide.PROPERTY_NAME_OBJECTIVE_POWER) or 0) or None,
        "mpp": mpp,
        "width": width,
        "height": height,
        "levels": dz.level_count,
        "tileSize": TILE_SIZE,
        "overlap": OVERLAP,
        "tileCount": len(jobs),
        "tilesBytes": sum(os.path.getsize(j[3]) for j in jobs),
        "rawSizeBytes": src.stat().st_size,
        "hotspots": cell_hotspots(thumb),
        "til": til,
        "dzi": f"slides/{sid}/slide.dzi",
        "thumbnail": f"slides/{sid}/thumbnail.jpg",
        "raw": f"raw/slides/{slide['file_name']}",
    }
    (sdir / "label.json").write_text(json.dumps(entry, indent=2))
    print(f"{pid}: {width}x{height}, {dz.level_count} levels, {len(jobs)} tiles, TIL {'yes' if til else 'no'} in {time.time() - t0:.0f}s", flush=True)
    return entry


def refresh_til() -> None:
    """Re-render only the TIL overlays and their placement, keeping the tile pyramids."""
    for label_path in sorted((OUT / "slides").glob("*/label.json")):
        entry = json.loads(label_path.read_text())
        pid, sdir = entry["patientId"], label_path.parent
        bounds_x = round(-(entry["til"] or {}).get("x", 0) * entry["width"]) if entry.get("til") else 0
        bounds_y = round(-(entry["til"] or {}).get("y", 0) * entry["width"]) if entry.get("til") else 0
        entry["til"] = til_overlay(pid, sdir, entry["mpp"], (bounds_x, bounds_y), entry["width"], entry["height"])
        label_path.write_text(json.dumps(entry, indent=2))
        print(f"{pid}: TIL {'refreshed' if entry['til'] else 'none'}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--til-only", action="store_true", help="re-render TIL overlays without re-tiling")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--tissue", action="store_true", help="tile the frozen-section slides listed under tissueSlides")
    args = ap.parse_args()
    if args.til_only:
        refresh_til()
        return
    index = json.loads((ROOT / "work" / "imaging_index.json").read_text())
    for pid, entry in index.items():
        if args.only and pid not in args.only:
            continue
        if args.tissue:
            for slide in entry.get("tissueSlides", []):
                section = slide["file_name"].split(".")[0].lower().removeprefix(pid.lower() + "-")
                if (OUT / "slides" / pid.lower() / section / "label.json").exists():
                    continue
                process(pid, slide, args.workers, section)
            continue
        process(pid, entry["slide"], args.workers)


if __name__ == "__main__":
    main()
