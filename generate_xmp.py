#!/usr/bin/env python3
"""
XMP Sidecar Generator

Extracts Camera Raw Settings from photographer-edited JPGs and generates
.xmp sidecar files for CR3 RAW files so Lightroom auto-applies the style on import.
"""

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from statistics import median, stdev
from textwrap import dedent

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


def extract_crs_from_jpgs(jpg_dir: Path) -> list[dict]:
    """
    Extract all XMP Camera Raw Settings from JPGs using exiftool.

    Runs a single batch exiftool call and parses the JSON output.

    Args:
        jpg_dir: Directory containing photographer-edited JPGs

    Returns:
        List of dicts, each mapping CRS tag name -> value for one JPG
    """
    jpg_files = sorted(jpg_dir.glob("*.jpg")) + sorted(jpg_dir.glob("*.JPG"))
    if not jpg_files:
        print(f"No JPG files found in {jpg_dir}")
        sys.exit(1)

    print(f"Found {len(jpg_files)} JPG files in {jpg_dir}")

    result = subprocess.run(
        ["exiftool", "-j", "-G1", "-XMP-crs:all"] + [str(f) for f in jpg_files],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"exiftool error: {result.stderr}")
        sys.exit(1)

    raw_entries = json.loads(result.stdout)

    # Strip "XMP-crs:" prefix from tag names
    cleaned = []
    for entry in raw_entries:
        d = {}
        for key, value in entry.items():
            if key.startswith("XMP-crs:"):
                tag_name = key[len("XMP-crs:"):]
                d[tag_name] = value
        if d:
            cleaned.append(d)

    print(f"Extracted CRS data from {len(cleaned)} files")
    return cleaned


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
    # All identical
    if len(set(values)) == 1:
        return "style", values[0], "identical across all files"

    med = median(values)
    if med == 0:
        # Can't compute CV with zero median; check if values are close to zero
        if all(abs(v) < 0.01 for v in values):
            return "style", 0, "all near zero"
        return "per-image", None, "varies (zero median, nonzero values)"

    # Coefficient of variation
    try:
        sd = stdev(values)
        cv = (sd / abs(med)) * 100
    except Exception:
        return "per-image", None, "could not compute variance"

    if cv < 10:
        return "style", round(med, 4), f"low variance (CV={cv:.1f}%)"
    return "per-image", None, f"high variance (CV={cv:.1f}%)"


def _classify_string(tag: str, values: list[str]) -> tuple[str, object, str]:
    """Classify a string tag based on agreement percentage."""
    counter = Counter(values)
    most_common_val, most_common_count = counter.most_common(1)[0]
    agreement = most_common_count / len(values)

    if agreement > 0.8:
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
) -> dict:
    """
    Write .xmp sidecar files for every CR3 in the directory.

    Args:
        style: Style settings dict
        cr3_dir: Directory containing CR3 files
        skip_existing: Skip if .xmp already exists
        dry_run: Print what would be done without writing

    Returns:
        Dict with counts: generated, skipped
    """
    cr3_files = sorted(cr3_dir.glob("*.CR3")) + sorted(cr3_dir.glob("*.cr3"))
    if not cr3_files:
        print(f"No CR3 files found in {cr3_dir}")
        return {"generated": 0, "skipped": 0}

    stats = {"generated": 0, "skipped": 0}

    for cr3_path in tqdm(cr3_files, desc="Generating XMP sidecars", unit="file"):
        xmp_path = cr3_path.with_suffix(".xmp")

        if skip_existing and xmp_path.exists():
            stats["skipped"] += 1
            continue

        if dry_run:
            stats["generated"] += 1
            continue

        xmp_content = build_xmp_sidecar(style, cr3_path.name)
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

    args = parser.parse_args()

    if not args.jpg_dir.is_dir():
        print(f"Error: JPG directory does not exist: {args.jpg_dir}")
        sys.exit(1)

    # Step 1: Extract CRS from JPGs
    print("Step 1: Extracting Camera Raw Settings from JPGs...")
    all_crs = extract_crs_from_jpgs(args.jpg_dir)

    if not all_crs:
        print("No CRS data found in JPGs. Are these Lightroom-exported files?")
        sys.exit(1)

    # Step 2: Classify settings
    print("\nStep 2: Classifying style vs per-image settings...")
    style, report = classify_settings(all_crs)

    print_analysis_report(style, report)

    if args.analyze_only:
        print(f"\n{len(style)} style settings extracted. Use without --analyze-only to generate sidecars.")
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
    )

    print(f"\nDone! Generated: {stats['generated']}, Skipped: {stats['skipped']}")
    if args.dry_run:
        print("(Dry run — no files were written)")


if __name__ == "__main__":
    main()
