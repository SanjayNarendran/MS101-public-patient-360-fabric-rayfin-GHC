"""Convert each patient's chosen CT/MR series (and any AI segmentation) into browser-ready NIfTI.

Output per series: work/lakehouse/imaging/radiology/<id>/<series-key>/
  volume.nii.gz    int16 (CT in HU), cropped to the body, ~1.5 mm voxels
  labels.nii.gz    uint8 label map from the IDC AI segmentation (when available)
  labels.lut.json  NiiVue colour table for the labels
  thumbnail.jpg    coronal slice through the centre of the body
  series.json      acquisition metadata, AI-measured volumes, file sizes

Usage: python pipeline/build_radiology.py [--only TCGA-XX-XXXX]
"""
from __future__ import annotations

import argparse
import glob
import json
import pathlib
from collections import defaultdict

import numpy as np
import pydicom
import SimpleITK as sitk
from PIL import Image
from scipy import ndimage

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "work" / "raw"
OUT = ROOT / "work" / "lakehouse" / "imaging" / "radiology"
CT_SPACING = 1.5
CT_MAX_VOXELS = 26_000_000
MR_MAX_VOXELS = 24_000_000

LABEL_COLOURS = {
    "kidney": (214, 150, 120), "liver": (196, 120, 90), "lung": (120, 170, 210),
    "tumor": (242, 100, 25), "mass": (242, 100, 25), "nodule": (242, 100, 25), "cyst": (90, 160, 230),
}


def dicom_files(folder: pathlib.Path) -> list[pydicom.Dataset]:
    return [pydicom.dcmread(f, stop_before_pixels=True) for f in glob.glob(str(folder / "**" / "*.dcm"), recursive=True)]


def pick_phase(headers: list[pydicom.Dataset]) -> list[pydicom.Dataset]:
    """Multi-phase series repeat slice positions; keep the first complete phase."""
    positions = [tuple(round(float(v), 2) for v in h.ImagePositionPatient) for h in headers]
    if len(set(positions)) == len(positions):
        return headers
    groups: dict[object, list] = defaultdict(list)
    for h in headers:
        key = h.get("TemporalPositionIdentifier") or h.get("AcquisitionNumber") or h.get("TriggerTime") or 0
        groups[key].append(h)
    complete = [g for g in groups.values() if len({tuple(round(float(v), 2) for v in h.ImagePositionPatient) for h in g}) == len(g)]
    if complete:
        return sorted(complete, key=lambda g: (-len(g), str(g[0].get("TemporalPositionIdentifier", ""))))[0]
    seen, first = set(), []
    for h in sorted(headers, key=lambda h: int(h.InstanceNumber)):
        pos = tuple(round(float(v), 2) for v in h.ImagePositionPatient)
        if pos not in seen:
            seen.add(pos)
            first.append(h)
    return first


def read_series(folder: pathlib.Path) -> tuple[sitk.Image, list[pydicom.Dataset]]:
    headers = pick_phase(dicom_files(folder))
    iop = np.array([float(v) for v in headers[0].ImageOrientationPatient])
    normal = np.cross(iop[:3], iop[3:])
    headers.sort(key=lambda h: float(np.dot(normal, [float(v) for v in h.ImagePositionPatient])))
    reader = sitk.ImageSeriesReader()
    reader.SetFileNames([h.filename for h in headers])
    return reader.Execute(), headers


def _frame_geometry(d: pydicom.Dataset, fg: pydicom.Dataset, name: str) -> pydicom.Dataset:
    """A functional group for one frame: per-frame if present, else shared."""
    if name in fg:
        return fg[name].value[0]
    return d.SharedFunctionalGroupsSequence[0][name].value[0]


def read_seg(path: str, reference: sitk.Image) -> tuple[sitk.Image, list[str]]:
    """Rasterise a multi-segment DICOM SEG onto the reference image grid.

    Each labelled pixel is placed through the SEG's own geometry (position, orientation and pixel spacing of its
    frame), because a SEG need not share the CT's pixel layout: several IDC segmentations store rows running
    posterior to anterior, the opposite of the CT they were drawn on.
    """
    d = pydicom.dcmread(path)
    frames = d.pixel_array
    if frames.ndim == 2:
        frames = frames[None]
    names = [str(s.SegmentLabel) for s in d.SegmentSequence]
    labelmap = str(d.get("SegmentationType", "")).upper() == "LABELMAP"
    labels = np.zeros(sitk.GetArrayFromImage(reference).shape, dtype=np.uint8)
    origin = np.array(reference.GetOrigin())
    to_index = np.array(reference.GetDirection()).reshape(3, 3).T / np.array(reference.GetSpacing())[:, None]
    for i, fg in enumerate(d.PerFrameFunctionalGroupsSequence):
        frame = frames[i]
        rr, cc = np.nonzero(frame)
        if not len(rr):
            continue
        pos = np.array([float(v) for v in fg.PlanePositionSequence[0].ImagePositionPatient])
        iop = [float(v) for v in _frame_geometry(d, fg, "PlaneOrientationSequence").ImageOrientationPatient]
        row_spacing, col_spacing = (float(v) for v in _frame_geometry(d, fg, "PixelMeasuresSequence").PixelSpacing)
        # DICOM: the first direction cosine runs along a row (column index increases), the second down a column.
        points = pos + np.outer(cc * col_spacing, iop[:3]) + np.outer(rr * row_spacing, iop[3:])
        idx = np.rint((points - origin) @ to_index.T).astype(int)
        x, y, z = idx[:, 0], idx[:, 1], idx[:, 2]
        keep = (x >= 0) & (x < labels.shape[2]) & (y >= 0) & (y < labels.shape[1]) & (z >= 0) & (z < labels.shape[0])
        x, y, z = x[keep], y[keep], z[keep]
        if labelmap:
            values = frame[rr[keep], cc[keep]].astype(np.uint8)
            labels[z, y, x] = np.maximum(labels[z, y, x], values)
        else:
            labels[z, y, x] = int(fg.SegmentIdentificationSequence[0].ReferencedSegmentNumber)
    if labelmap:
        numbers = [int(s.SegmentNumber) for s in d.SegmentSequence]
        names = [""] * max(numbers)
        for s in d.SegmentSequence:
            names[int(s.SegmentNumber) - 1] = str(s.SegmentLabel)
    img = sitk.GetImageFromArray(labels)
    img.CopyInformation(reference)
    return img, names


def crop_box(mask: np.ndarray, margin: int = 3) -> tuple[list[int], list[int]]:
    zz, yy, xx = np.nonzero(mask)
    shape = mask.shape
    lo = [max(0, int(a.min()) - margin) for a in (xx, yy, zz)]
    hi = [min(s, int(a.max()) + 1 + margin) for a, s in zip((xx, yy, zz), (shape[2], shape[1], shape[0]))]
    return lo, [h - l for l, h in zip(lo, hi)]


def body_mask(arr: np.ndarray, threshold: float) -> np.ndarray:
    m = ndimage.binary_opening(arr > threshold, iterations=2)
    lab, k = ndimage.label(m)
    if k == 0:
        return np.ones_like(m)
    return lab == (1 + int(np.argmax(ndimage.sum(m, lab, range(1, k + 1)))))


def thumbnail(arr: np.ndarray, spacing: tuple[float, float, float], is_ct: bool, dest: pathlib.Path) -> None:
    sl = arr[:, arr.shape[1] // 2, :].astype(np.float32)  # coronal (z, x)
    if is_ct:
        lo, hi = -160, 240
    else:
        lo, hi = np.percentile(sl, 1), np.percentile(sl, 99.5)
    img = (np.clip((sl - lo) / max(hi - lo, 1), 0, 1) * 255).astype(np.uint8)[::-1]
    pil = Image.fromarray(img).resize((img.shape[1], max(1, int(img.shape[0] * spacing[2] / spacing[0]))))
    pil.thumbnail((360, 360))
    canvas = Image.new("L", (360, 360), 0)
    canvas.paste(pil, ((360 - pil.width) // 2, (360 - pil.height) // 2))
    canvas.convert("RGB").save(dest, quality=88)


def lut_for(names: list[str]) -> dict:
    r, g, b, a = [0], [0], [0], [0]
    for n in names:
        colour = next((c for k, c in LABEL_COLOURS.items() if k in n.lower()), (200, 200, 200))
        r.append(colour[0]); g.append(colour[1]); b.append(colour[2]); a.append(255)
    return {"R": r, "G": g, "B": b, "A": a, "I": list(range(len(names) + 1)), "labels": [""] + names}


def build_series(pid: str, series: dict, key: str) -> dict:
    src = RAW / "dicom" / pid / series["seriesUid"]
    img, headers = read_series(src)
    h0 = headers[0]
    is_ct = series["modality"] == "CT"
    dest = OUT / pid.lower() / key
    dest.mkdir(parents=True, exist_ok=True)

    seg_img, seg_names, seg_meta = None, [], None
    for seg in series.get("aiSegmentations", []):
        files = glob.glob(str(RAW / "seg" / pid / seg["seg"] / "**" / "*.dcm"), recursive=True)
        if files:
            seg_img, seg_names = read_seg(files[0], img)
            seg_meta = seg
            break

    if is_ct:
        extent_mm = np.array(img.GetSize()) * np.array(img.GetSpacing())
        voxels = np.prod(extent_mm / CT_SPACING)
        spacing = [CT_SPACING * max(1.0, (voxels / CT_MAX_VOXELS) ** (1 / 3))] * 3
    else:
        sp = np.array(img.GetSpacing())
        voxels = np.prod(img.GetSize())
        scale = max(1.0, (voxels / MR_MAX_VOXELS) ** (1 / 3))
        spacing = list(sp * scale)
    size = [int(round(sz * sp / ns)) for sz, sp, ns in zip(img.GetSize(), img.GetSpacing(), spacing)]
    fill = -1024 if is_ct else 0
    out = sitk.Resample(img, size, sitk.Transform(), sitk.sitkLinear, img.GetOrigin(), spacing, img.GetDirection(), fill, sitk.sitkFloat32)
    arr = sitk.GetArrayFromImage(out)
    mask = body_mask(arr, -500 if is_ct else np.percentile(arr, 60))
    lo, extent = crop_box(mask)
    out = sitk.RegionOfInterest(out, extent, lo)
    out = sitk.Cast(out if is_ct else sitk.RescaleIntensity(out, 0, 4000), sitk.sitkInt16)
    sitk.WriteImage(out, str(dest / "volume.nii.gz"), useCompression=True)

    measures = {}
    if seg_img is not None:
        lab = sitk.Resample(seg_img, out, sitk.Transform(), sitk.sitkNearestNeighbor, 0, sitk.sitkUInt8)
        sitk.WriteImage(lab, str(dest / "labels.nii.gz"), useCompression=True)
        (dest / "labels.lut.json").write_text(json.dumps(lut_for(seg_names)))
        native = sitk.GetArrayFromImage(seg_img)
        voxel_ml = float(np.prod(seg_img.GetSpacing())) / 1000.0
        for i, name in enumerate(seg_names, start=1):
            n = int((native == i).sum())
            if n:
                comps = int(ndimage.label(native == i)[1])
                measures[name] = {"volumeMl": round(n * voxel_ml, 1), "components": comps}

    a = sitk.GetArrayFromImage(out)
    thumbnail(a, out.GetSpacing(), is_ct, dest / "thumbnail.jpg")
    raw_files = list(src.rglob("*.dcm"))
    meta = {
        "patientId": pid,
        "key": key,
        "role": series["role"],
        "seriesUid": series["seriesUid"],
        "studyUid": series["studyUid"],
        "studyDateDeidentified": series["date"],
        "modality": series["modality"],
        "description": series["description"].strip(),
        "bodyPart": h0.get("BodyPartExamined"),
        "manufacturer": h0.get("Manufacturer"),
        "model": h0.get("ManufacturerModelName"),
        "sliceThicknessMm": float(h0.get("SliceThickness") or 0) or None,
        "kvp": float(h0.get("KVP") or 0) or None,
        "contrast": str(h0.get("ContrastBolusAgent") or "") or None,
        "fieldStrengthT": float(h0.get("MagneticFieldStrength") or 0) or None,
        "sequence": str(h0.get("SequenceName") or h0.get("ScanningSequence") or "") or None,
        "images": len(headers),
        "dims": list(out.GetSize()),
        "spacingMm": [round(s, 3) for s in out.GetSpacing()],
        "volume": f"radiology/{pid.lower()}/{key}/volume.nii.gz",
        "labels": f"radiology/{pid.lower()}/{key}/labels.nii.gz" if seg_img is not None else None,
        "labelsLut": f"radiology/{pid.lower()}/{key}/labels.lut.json" if seg_img is not None else None,
        "thumbnail": f"radiology/{pid.lower()}/{key}/thumbnail.jpg",
        "aiSegmentation": {**seg_meta, "measures": measures} if seg_meta else None,
        "volumeBytes": (dest / "volume.nii.gz").stat().st_size,
        "rawFiles": len(raw_files),
        "rawBytes": sum(f.stat().st_size for f in raw_files),
        "rawPath": f"raw/dicom/{pid}/{series['seriesUid']}",
        "sourceUrl": f"https://viewer.imaging.datacommons.cancer.gov/v3/viewer/?StudyInstanceUIDs={series['studyUid']}",
    }
    (dest / "series.json").write_text(json.dumps(meta, indent=2, default=str))
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    index = json.loads((ROOT / "work" / "imaging_index.json").read_text())
    for pid, entry in index.items():
        if args.only and pid not in args.only:
            continue
        for n, series in enumerate(entry["series"]):
            key = "primary" if series["role"] == "primary" else f"followup-{n}"
            m = build_series(pid, series, key)
            seg = m["aiSegmentation"]["measures"] if m["aiSegmentation"] else ""
            print(f"{pid} {key}: {m['modality']} {m['description']} -> {m['dims']} @ {m['spacingMm']}, "
                  f"{m['volumeBytes'] / 1e6:.1f} MB {seg}", flush=True)


if __name__ == "__main__":
    main()
