#!/usr/bin/env python3
"""
XMP Sidecar Generator

Extracts Camera Raw Settings from photographer-edited JPGs and generates
.xmp sidecar files for CR3 RAW files so Lightroom auto-applies the style on import.
"""

import argparse
import json
import math
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import median, stdev
from textwrap import dedent

import exiftool
from tqdm import tqdm

# Tags that are always per-image (never part of the style)
PER_IMAGE_TAGS = {
    "Exposure2012",
    "Temperature",
    "Tint",
    "WhiteBalance",
    "CropTop",
    "CropLeft",
    "CropBottom",
    "CropRight",
    "CropAngle",
    "HasCrop",
    "MaskGroupBasedCorrections",
    "GrainSeed",
    "RawFileName",
    "AlreadyApplied",
}

# Tags that are always part of the style (even if they vary slightly)
STYLE_TAGS = {
    "RedHue",
    "RedSaturation",
    "GreenHue",
    "GreenSaturation",
    "BlueHue",
    "BlueSaturation",
    "GrainAmount",
    "GrainSize",
    "GrainFrequency",
    "ProcessVersion",
    "CameraProfile",
}

# Tags that contain tone curve point lists
TONE_CURVE_TAGS = {
    "ToneCurvePV2012",
    "ToneCurvePV2012Red",
    "ToneCurvePV2012Green",
    "ToneCurvePV2012Blue",
}


def extract_crs_from_jpgs(jpg_dir: Path, et: exiftool.ExifTool) -> tuple[list[dict], list[str]]:
    """
    Extract all XMP Camera Raw Settings from JPGs using exiftool.

    Args:
        jpg_dir: Directory containing photographer-edited JPGs
        et: Running ExifTool instance

    Returns:
        Tuple of (list of CRS dicts, list of source filenames)
    """
    jpg_files = sorted(jpg_dir.glob("*.jpg")) + sorted(jpg_dir.glob("*.JPG"))
    if not jpg_files:
        print(f"No JPG files found in {jpg_dir}")
        sys.exit(1)

    print(f"Found {len(jpg_files)} JPG files in {jpg_dir}")

    raw_entries = et.execute_json("-XMP-crs:all", *[str(f) for f in jpg_files])

    # Strip "XMP:" prefix from tag names, track source filenames
    cleaned = []
    source_files = []
    for entry in raw_entries:
        d = {}
        for key, value in entry.items():
            if key.startswith("XMP:"):
                tag_name = key[len("XMP:"):]
                d[tag_name] = value
        if d:
            cleaned.append(d)
            source_files.append(entry.get("SourceFile", ""))

    print(f"Extracted CRS data from {len(cleaned)} files")
    return cleaned, source_files


def extract_cr3_metadata(
    cr3_files: list[Path], workers: int = 4,
) -> dict[str, dict]:
    """
    Extract timestamps and shooting EXIF from all CR3 files in one pass.

    Runs multiple exiftool processes in parallel for speed.

    Args:
        cr3_files: List of CR3 file paths
        workers: Number of parallel exiftool processes

    Returns:
        Dict mapping CR3 stem (lowercase) to metadata dict with keys:
        datetime, ISO, ExposureTime, FNumber, FocalLength, Flash
    """
    if not cr3_files:
        return {}

    chunk_size = 50
    file_strs = [str(f) for f in cr3_files]
    chunks = [file_strs[i:i + chunk_size] for i in range(0, len(file_strs), chunk_size)]

    def process_chunk(chunk: list[str]) -> dict[str, dict]:
        result = {}
        with exiftool.ExifTool() as et:
            entries = et.execute_json(
                "-n", "-DateTimeOriginal", "-SubSecTimeOriginal",
                "-ISO", "-ExposureTime", "-FNumber", "-FocalLength", "-Flash",
                *chunk,
            )
            for entry in entries:
                source = entry.get("SourceFile", "")
                stem = Path(source).stem.lower()
                dt = entry.get("EXIF:DateTimeOriginal", "")
                subsec = str(entry.get("EXIF:SubSecTimeOriginal", ""))
                result[stem] = {
                    "datetime": f"{dt}.{subsec}" if dt and subsec else dt,
                    "ISO": entry.get("EXIF:ISO", 0),
                    "ExposureTime": entry.get("EXIF:ExposureTime", 0),
                    "FNumber": entry.get("EXIF:FNumber", 0),
                    "FocalLength": entry.get("EXIF:FocalLength", 0),
                    "Flash": 1 if entry.get("EXIF:Flash", 0) else 0,
                }
        return result

    metadata = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_chunk, chunk): len(chunk) for chunk in chunks}
        with tqdm(total=len(cr3_files), desc="  Reading metadata from CR3s", unit="file") as pbar:
            for future in as_completed(futures):
                metadata.update(future.result())
                pbar.update(futures[future])

    return metadata


def classify_settings(all_crs: list[dict]) -> tuple[dict, list[dict]]:
    """
    Separate style settings (consistent across images) from per-image settings.

    Algorithm:
    - Hardcoded exclusions -> per-image
    - Hardcoded inclusions -> style (use median for numeric)
    - Remaining: data-driven classification based on variance

    Args:
        all_crs: List of CRS tag dicts from extract_crs_from_jpgs()

    Returns:
        Tuple of (style_settings dict, per_image_report list of dicts)
    """
    # Collect all unique tag names
    all_tags = set()
    for crs in all_crs:
        all_tags.update(crs.keys())

    style = {}
    report = []
    n = len(all_crs)

    for tag in sorted(all_tags):
        # Gather values for this tag across all files
        values = [crs[tag] for crs in all_crs if tag in crs]
        if not values:
            continue

        present_count = len(values)

        # Hardcoded per-image exclusions
        if tag in PER_IMAGE_TAGS:
            report.append({
                "tag": tag,
                "classification": "per-image (excluded)",
                "reason": "hardcoded exclusion",
                "sample": values[0],
            })
            continue

        # Hardcoded style inclusions
        if tag in STYLE_TAGS:
            value = _pick_representative(values)
            style[tag] = value
            report.append({
                "tag": tag,
                "classification": "style (forced)",
                "reason": "hardcoded inclusion",
                "value": value,
            })
            continue

        # Tone curve tags - use most common
        if tag in TONE_CURVE_TAGS:
            value = _pick_most_common_list(values)
            style[tag] = value
            report.append({
                "tag": tag,
                "classification": "style (tone curve)",
                "reason": "most common curve",
                "value": str(value)[:80] + "..." if len(str(value)) > 80 else str(value),
            })
            continue

        # Data-driven classification
        sample = values[0]

        if isinstance(sample, (int, float)):
            classification, value, reason = _classify_numeric(tag, values)
        elif isinstance(sample, str):
            classification, value, reason = _classify_string(tag, values)
        elif isinstance(sample, list):
            classification, value, reason = _classify_list(tag, values)
        else:
            # Unknown type, skip
            report.append({
                "tag": tag,
                "classification": "skipped",
                "reason": f"unknown type: {type(sample).__name__}",
            })
            continue

        if classification == "style":
            style[tag] = value
        report.append({
            "tag": tag,
            "classification": classification,
            "reason": reason,
            "value": value if classification == "style" else None,
            "present": f"{present_count}/{n}",
        })

    return style, report


def _pick_representative(values: list) -> object:
    """Pick a representative value: median for numeric, most common for others."""
    if not values:
        return None
    if isinstance(values[0], (int, float)):
        return round(median(values), 4)
    # Most common for strings
    counter = Counter(str(v) for v in values)
    most_common_str = counter.most_common(1)[0][0]
    # Return original value matching the most common string
    for v in values:
        if str(v) == most_common_str:
            return v
    return values[0]


def _pick_most_common_list(values: list[list]) -> list:
    """Pick the most common list value (by string representation)."""
    counter = Counter(json.dumps(v, sort_keys=True) if isinstance(v, list) else str(v) for v in values)
    most_common_str = counter.most_common(1)[0][0]
    for v in values:
        serialized = json.dumps(v, sort_keys=True) if isinstance(v, list) else str(v)
        if serialized == most_common_str:
            return v
    return values[0]


def _classify_numeric(tag: str, values: list) -> tuple[str, object, str]:
    """Classify a numeric tag based on variance."""
    # Coerce to float, dropping any non-numeric values
    numeric = []
    for v in values:
        if isinstance(v, (int, float)):
            numeric.append(float(v))
        elif isinstance(v, str):
            try:
                numeric.append(float(v))
            except ValueError:
                continue
    if not numeric:
        return "per-image", None, "no numeric values after coercion"

    # All identical
    if len(set(numeric)) == 1:
        return "style", numeric[0], "identical across all files"

    med = median(numeric)
    if med == 0:
        # Can't compute CV with zero median; check if values are close to zero
        if all(abs(v) < 0.01 for v in numeric):
            return "style", 0, "all near zero"
        return "per-image", None, "varies (zero median, nonzero values)"

    # Coefficient of variation
    try:
        sd = stdev(numeric)
        cv = (sd / abs(med)) * 100
    except Exception:
        return "per-image", None, "could not compute variance"

    if cv < 10:
        return "style", round(med, 4), f"low variance (CV={cv:.1f}%)"
    return "per-image", None, f"high variance (CV={cv:.1f}%)"


def _classify_string(tag: str, values: list[str]) -> tuple[str, object, str]:
    """Classify a string tag based on agreement percentage."""
    # Coerce all values to strings for consistent hashing
    str_values = [str(v) for v in values]
    counter = Counter(str_values)
    most_common_val, most_common_count = counter.most_common(1)[0]
    agreement = most_common_count / len(str_values)

    if agreement > 0.8:
        # Return the original value matching the most common string
        for v, s in zip(values, str_values):
            if s == most_common_val:
                return "style", v, f"{agreement:.0%} agreement"
        return "style", most_common_val, f"{agreement:.0%} agreement"
    return "per-image", None, f"only {agreement:.0%} agreement"


def _classify_list(tag: str, values: list[list]) -> tuple[str, object, str]:
    """Classify a list tag based on identity."""
    serialized = [json.dumps(v, sort_keys=True) for v in values]
    counter = Counter(serialized)
    most_common_str, most_common_count = counter.most_common(1)[0]
    agreement = most_common_count / len(values)

    if agreement > 0.8:
        # Return the actual list, not the serialized string
        for v, s in zip(values, serialized):
            if s == most_common_str:
                return "style", v, f"{agreement:.0%} identical"
        return "style", values[0], f"{agreement:.0%} identical"
    return "per-image", None, f"only {agreement:.0%} identical"


def _merge_crs(crs_list: list[dict]) -> dict:
    """
    Merge multiple CRS dicts into one.

    - Numeric values: mean
    - String values: most common
    - List values: most common by JSON serialization
    """
    if not crs_list:
        return {}
    if len(crs_list) == 1:
        return dict(crs_list[0])

    all_tags = set()
    for crs in crs_list:
        all_tags.update(crs.keys())

    merged = {}
    for tag in all_tags:
        values = [crs[tag] for crs in crs_list if tag in crs]
        if not values:
            continue

        # Try to average as numeric; fall back to most-common if any value isn't a number
        numeric = []
        for v in values:
            if isinstance(v, (int, float)):
                numeric.append(float(v))
            elif isinstance(v, str):
                try:
                    numeric.append(float(v))
                except ValueError:
                    break
            else:
                break

        if len(numeric) == len(values):
            merged[tag] = round(sum(numeric) / len(numeric), 4)
        elif isinstance(values[0], list):
            merged[tag] = _pick_most_common_list(values)
        else:
            # String or other: most common
            counter = Counter(str(v) for v in values)
            most_common_str = counter.most_common(1)[0][0]
            for v in values:
                if str(v) == most_common_str:
                    merged[tag] = v
                    break
            else:
                merged[tag] = values[0]

    return merged


def _extract_datetimes(
    file_paths: list[str], label: str = "files", workers: int = 4,
) -> dict[str, str]:
    """
    Extract DateTimeOriginal + SubSecTimeOriginal from files via exiftool.

    Runs multiple exiftool processes in parallel for speed.

    Returns:
        Dict mapping file path -> composite datetime string
    """
    if not file_paths:
        return {}

    chunk_size = 50
    chunks = [file_paths[i:i + chunk_size] for i in range(0, len(file_paths), chunk_size)]

    def process_chunk(chunk: list[str]) -> dict[str, str]:
        result = {}
        with exiftool.ExifTool() as et:
            entries = et.execute_json("-DateTimeOriginal", "-SubSecTimeOriginal", *chunk)
            for entry in entries:
                src = entry.get("SourceFile", "")
                dt = entry.get("EXIF:DateTimeOriginal", "")
                if dt:
                    subsec = str(entry.get("EXIF:SubSecTimeOriginal", ""))
                    result[src] = f"{dt}.{subsec}" if subsec else dt
        return result

    dt_map = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_chunk, chunk): len(chunk) for chunk in chunks}
        with tqdm(total=len(file_paths), desc=f"  Reading timestamps from {label}", unit="file") as pbar:
            for future in as_completed(futures):
                dt_map.update(future.result())
                pbar.update(futures[future])

    return dt_map


def match_and_calibrate(
    all_crs: list[dict],
    source_files: list[str],
    cr3_dir: Path,
) -> dict[str, dict]:
    """
    Match JPGs to CR3s by DateTimeOriginal; matched CR3s get full CRS,
    unmatched CR3s get nearest-neighbor averaged CRS from matched CR3s.

    Args:
        all_crs: List of CRS dicts from extract_crs_from_jpgs()
        source_files: Parallel list of source filenames
        cr3_dir: Directory containing CR3 files

    Returns:
        Dict mapping CR3 stem (lowercase) to CRS settings dict
    """
    # Get all CR3 files
    cr3_files = sorted(cr3_dir.glob("*.CR3")) + sorted(cr3_dir.glob("*.cr3"))
    all_cr3_stems = {f.stem.lower() for f in cr3_files}

    # Single pass: extract timestamps + shooting EXIF from all CR3s
    cr3_metadata = extract_cr3_metadata(cr3_files)

    # Extract timestamps from JPGs (small set, fast)
    jpg_datetimes = _extract_datetimes(source_files, label="JPGs")

    # Build CR3 datetime -> stem lookup
    cr3_dt_to_stem: dict[str, str] = {}
    for stem, meta in cr3_metadata.items():
        dt = meta.get("datetime", "")
        if dt:
            cr3_dt_to_stem[dt] = stem

    # Match JPGs to CR3s by DateTimeOriginal
    matched_stems: set[str] = set()
    matched_jpgs: set[str] = set()
    # jpg_by_stem stores matched CR3 stem -> CRS from its paired JPG
    jpg_by_stem: dict[str, dict] = {}

    for crs, src in zip(all_crs, source_files):
        dt = jpg_datetimes.get(src)
        if not dt:
            continue
        cr3_stem = cr3_dt_to_stem.get(dt)
        if cr3_stem:
            matched_stems.add(cr3_stem)
            matched_jpgs.add(src)
            jpg_by_stem[cr3_stem] = crs

    unmatched_jpg_count = len(source_files) - len(matched_jpgs)
    unmatched_cr3s = all_cr3_stems - matched_stems

    # Print matching report
    print(f"\n{'=' * 60}")
    print("CALIBRATION MATCHING REPORT")
    print(f"{'=' * 60}")
    print(f"  Matched pairs:   {len(matched_stems)} (by DateTimeOriginal)")
    print(f"  Unmatched JPGs:  {unmatched_jpg_count}")
    print(f"  Unmatched CR3s:  {len(unmatched_cr3s)}")
    if matched_stems:
        print(f"\n  Matched CR3 stems: {', '.join(sorted(matched_stems))}")
    print(f"{'=' * 60}")

    if not matched_stems:
        print("\nWarning: No JPG-CR3 matches found. Falling back to uniform style.")
        return {}

    # Build per-CR3 styles for matched CR3s
    per_cr3_styles: dict[str, dict] = {}
    for stem in matched_stems:
        per_cr3_styles[stem] = jpg_by_stem[stem]

    if not unmatched_cr3s:
        return per_cr3_styles

    # Use already-extracted EXIF for nearest-neighbor (no second scan needed)
    print("\nComputing nearest neighbors from cached EXIF data...")
    exif_data = {
        stem: {k: v for k, v in meta.items() if k != "datetime"}
        for stem, meta in cr3_metadata.items()
    }

    # Normalize EXIF features to [0, 1] via min-max
    features = ["ISO", "ExposureTime", "FNumber", "FocalLength", "Flash"]
    all_stems_with_exif = set(exif_data.keys())

    # Compute min/max for each feature
    feat_min = {}
    feat_max = {}
    for feat in features:
        vals = [exif_data[s][feat] for s in all_stems_with_exif if feat in exif_data[s]]
        if vals:
            feat_min[feat] = min(vals)
            feat_max[feat] = max(vals)
        else:
            feat_min[feat] = 0
            feat_max[feat] = 1

    def normalize(stem: str) -> list[float]:
        """Get normalized feature vector for a CR3."""
        exif = exif_data.get(stem, {})
        vec = []
        for feat in features:
            val = exif.get(feat, 0)
            fmin = feat_min[feat]
            fmax = feat_max[feat]
            if fmax - fmin > 0:
                vec.append((val - fmin) / (fmax - fmin))
            else:
                vec.append(0.0)
        return vec

    def euclidean_dist(a: list[float], b: list[float]) -> float:
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

    # For each unmatched CR3, find k=5 nearest matched CR3s
    k = min(5, len(matched_stems))
    matched_with_exif = [s for s in matched_stems if s in exif_data]

    if not matched_with_exif:
        print("Warning: No EXIF data for matched CR3s. Unmatched CR3s will use fallback style.")
        return per_cr3_styles

    matched_vecs = {s: normalize(s) for s in matched_with_exif}

    for stem in tqdm(sorted(unmatched_cr3s), desc="Finding nearest neighbors", unit="file"):
        if stem not in exif_data:
            continue
        target_vec = normalize(stem)
        # Compute distances to all matched CR3s
        distances = []
        for m_stem in matched_with_exif:
            dist = euclidean_dist(target_vec, matched_vecs[m_stem])
            distances.append((dist, m_stem))
        distances.sort(key=lambda x: x[0])
        neighbors = [m_stem for _, m_stem in distances[:k]]
        # Merge neighbor CRS settings
        neighbor_crs = [jpg_by_stem[s] for s in neighbors]
        per_cr3_styles[stem] = _merge_crs(neighbor_crs)

    return per_cr3_styles


def build_xmp_sidecar(style: dict, raw_filename: str) -> str:
    """
    Generate an XMP sidecar XML string for a single RAW file.

    Args:
        style: Dict of CRS tag name -> value (from classify_settings)
        raw_filename: Name of the RAW file (e.g., "1758871217.CR3")

    Returns:
        Complete XMP XML string
    """
    # Separate tone curve tags from scalar tags
    curve_tags = {}
    scalar_tags = {}
    for tag, value in style.items():
        if tag in TONE_CURVE_TAGS:
            curve_tags[tag] = value
        else:
            scalar_tags[tag] = value

    # Build crs:attributes string for scalar values
    attr_lines = []
    for tag in sorted(scalar_tags):
        value = scalar_tags[tag]
        # Format numeric values
        if isinstance(value, float):
            # Use sign prefix for certain adjustment tags
            formatted = f"{value:+.2f}" if _is_signed_tag(tag) else f"{value:.2f}"
            # Remove unnecessary trailing zeros but keep at least two decimal places
        elif isinstance(value, bool):
            formatted = "True" if value else "False"
        else:
            formatted = str(value)
        attr_lines.append(f'   crs:{tag}="{formatted}"')

    # Override defaults for neutral starting point
    overrides = {
        "AlreadyApplied": "False",
        "WhiteBalance": "As Shot",
        "Exposure2012": "0.00",
    }
    for tag, value in overrides.items():
        # Remove any existing entry for this tag
        attr_lines = [l for l in attr_lines if f"crs:{tag}=" not in l]
        attr_lines.append(f'   crs:{tag}="{value}"')

    attr_lines.sort()
    attrs_str = "\n".join(attr_lines)

    # Build tone curve elements
    curve_elements = ""
    for tag in sorted(curve_tags):
        points = curve_tags[tag]
        if not isinstance(points, list):
            continue
        li_items = "\n".join(f"       <rdf:li>{point}</rdf:li>" for point in points)
        curve_elements += dedent(f"""\
      <crs:{tag}>
       <rdf:Seq>
{li_items}
       </rdf:Seq>
      </crs:{tag}>
""")

    xmp = dedent(f"""\
        <x:xmpmeta xmlns:x="adobe:ns:meta/">
         <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
          <rdf:Description rdf:about=""
           xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
{attrs_str}>
{curve_elements}  </rdf:Description>
         </rdf:RDF>
        </x:xmpmeta>
    """)

    return xmp


def _is_signed_tag(tag: str) -> bool:
    """Check if a tag should display with explicit +/- sign."""
    signed_tags = {
        "Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012",
        "Whites2012", "Blacks2012", "Clarity2012", "Vibrance", "Saturation",
        "ParametricShadows", "ParametricDarks", "ParametricLights",
        "ParametricHighlights", "SharpenDetail", "SharpenEdgeMasking",
        "PostCropVignetteAmount", "SplitToningBalance",
        "RedHue", "RedSaturation", "GreenHue", "GreenSaturation",
        "BlueHue", "BlueSaturation",
    }
    return tag in signed_tags


def generate_sidecars(
    style: dict,
    cr3_dir: Path,
    skip_existing: bool = False,
    dry_run: bool = False,
    per_cr3_styles: dict[str, dict] | None = None,
) -> dict:
    """
    Write .xmp sidecar files for every CR3 in the directory.

    Args:
        style: Fallback style settings dict
        cr3_dir: Directory containing CR3 files
        skip_existing: Skip if .xmp already exists
        dry_run: Print what would be done without writing
        per_cr3_styles: Optional dict mapping CR3 stem (lowercase) to per-file CRS

    Returns:
        Dict with counts: generated, skipped, calibrated
    """
    cr3_files = sorted(cr3_dir.glob("*.CR3")) + sorted(cr3_dir.glob("*.cr3"))
    if not cr3_files:
        print(f"No CR3 files found in {cr3_dir}")
        return {"generated": 0, "skipped": 0, "calibrated": 0}

    stats = {"generated": 0, "skipped": 0, "calibrated": 0}

    for cr3_path in tqdm(cr3_files, desc="Generating XMP sidecars", unit="file"):
        xmp_path = cr3_path.with_suffix(".xmp")

        if skip_existing and xmp_path.exists():
            stats["skipped"] += 1
            continue

        # Use per-CR3 style if available, otherwise fallback
        cr3_stem = cr3_path.stem.lower()
        if per_cr3_styles and cr3_stem in per_cr3_styles:
            file_style = per_cr3_styles[cr3_stem]
            stats["calibrated"] += 1
        else:
            file_style = style

        if dry_run:
            stats["generated"] += 1
            continue

        xmp_content = build_xmp_sidecar(file_style, cr3_path.name)
        xmp_path.write_text(xmp_content, encoding="utf-8")
        stats["generated"] += 1

    return stats


def print_analysis_report(style: dict, report: list[dict]):
    """Print a human-readable analysis of the extracted style."""
    print("\n" + "=" * 60)
    print("STYLE ANALYSIS REPORT")
    print("=" * 60)

    style_items = [r for r in report if r["classification"].startswith("style")]
    per_image_items = [r for r in report if r["classification"].startswith("per-image")]
    skipped_items = [r for r in report if r["classification"] == "skipped"]

    print(f"\nStyle settings ({len(style_items)} tags):")
    print("-" * 40)
    for item in style_items:
        value_str = str(item.get("value", ""))
        if len(value_str) > 60:
            value_str = value_str[:60] + "..."
        print(f"  {item['tag']:40s} = {value_str}")
        print(f"    {'':40s}   ({item['reason']})")

    print(f"\nPer-image settings ({len(per_image_items)} tags):")
    print("-" * 40)
    for item in per_image_items:
        print(f"  {item['tag']:40s} ({item['reason']})")

    if skipped_items:
        print(f"\nSkipped ({len(skipped_items)} tags):")
        print("-" * 40)
        for item in skipped_items:
            print(f"  {item['tag']:40s} ({item['reason']})")

    print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Extract photographer style from JPGs and generate XMP sidecars for CR3 files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=dedent("""\
            Example usage:
              %(prog)s --jpg-dir ./sneak_peeks --analyze-only
              %(prog)s --jpg-dir ./sneak_peeks --cr3-dir ./downloads
              %(prog)s --jpg-dir ./sneak_peeks --cr3-dir ./downloads --skip-existing --dry-run
        """),
    )

    parser.add_argument(
        "--jpg-dir",
        required=True,
        type=Path,
        help="Directory containing photographer-edited JPGs with embedded XMP CRS data",
    )
    parser.add_argument(
        "--cr3-dir",
        type=Path,
        default=Path("./downloads"),
        help="Directory containing CR3 RAW files (default: ./downloads)",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Extract and display style analysis without generating sidecars",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be generated without writing files",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip CR3 files that already have an .xmp sidecar",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Match JPGs to CR3s by filename; matched get full edit, unmatched get nearest-neighbor style",
    )

    args = parser.parse_args()

    if not args.jpg_dir.is_dir():
        print(f"Error: JPG directory does not exist: {args.jpg_dir}")
        sys.exit(1)

    with exiftool.ExifTool() as et:
        # Step 1: Extract CRS from JPGs
        print("Step 1: Extracting Camera Raw Settings from JPGs...")
        all_crs, source_files = extract_crs_from_jpgs(args.jpg_dir, et)

        if not all_crs:
            print("No CRS data found in JPGs. Are these Lightroom-exported files?")
            sys.exit(1)

        # Step 2: Calibration mode or uniform style
        per_cr3_styles = None

        if args.calibrate:
            if not args.cr3_dir.is_dir():
                print(f"Error: CR3 directory does not exist: {args.cr3_dir}")
                sys.exit(1)

            print("\nStep 2: Calibrating — matching JPGs to CR3s by timestamp...")
            per_cr3_styles = match_and_calibrate(all_crs, source_files, args.cr3_dir)

            # Classify the matched subset for analysis report and fallback style
            matched_crs = [crs for crs, src in zip(all_crs, source_files)
                           if Path(src).stem.lower() in per_cr3_styles]
            crs_for_classify = matched_crs if matched_crs else all_crs

            print("\nStep 2b: Classifying style from matched images (for analysis & fallback)...")
            style, report = classify_settings(crs_for_classify)
            print_analysis_report(style, report)
        else:
            print("\nStep 2: Classifying style vs per-image settings...")
            style, report = classify_settings(all_crs)
            print_analysis_report(style, report)

        if args.analyze_only:
            print(f"\n{len(style)} style settings extracted. Use without --analyze-only to generate sidecars.")
            if per_cr3_styles:
                print(f"Calibration: {len(per_cr3_styles)} CR3s have per-file styles.")
            return

        # Step 3: Generate sidecars
        if not args.cr3_dir.is_dir():
            print(f"Error: CR3 directory does not exist: {args.cr3_dir}")
            sys.exit(1)

        action = "Would generate" if args.dry_run else "Generating"
        print(f"\nStep 3: {action} XMP sidecars in {args.cr3_dir}...")

        stats = generate_sidecars(
            style=style,
            cr3_dir=args.cr3_dir,
            skip_existing=args.skip_existing,
            dry_run=args.dry_run,
            per_cr3_styles=per_cr3_styles,
        )

        print(f"\nDone! Generated: {stats['generated']}, Skipped: {stats['skipped']}")
        if per_cr3_styles:
            print(f"Calibrated: {stats['calibrated']} CR3s received per-file styles")
        if args.dry_run:
            print("(Dry run — no files were written)")


if __name__ == "__main__":
    main()
