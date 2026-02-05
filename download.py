#!/usr/bin/env python3
"""
Synology NAS Photo Downloader

Downloads photos from a Synology NAS through the gofile sharing interface.
Uses Playwright for browser-based authentication and requests for file downloads.
"""

import argparse
import shutil
import time
import urllib.parse
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright
from tqdm import tqdm


class IncompleteDownloadError(Exception):
    """Raised when a download does not complete fully."""

    pass


def authenticate(url: str, sharing_id: str, password: str) -> dict[str, str]:
    """
    Use Playwright to authenticate with the gofile sharing URL.

    Args:
        url: Base gofile URL (e.g., https://gofile-xxx.quickconnect.to)
        sharing_id: The sharing ID from URL path
        password: Password for the shared folder

    Returns:
        Dictionary containing cookies (type, sharing_sid)
    """
    full_url = f"{url}/sharing/{sharing_id}"
    print(f"Authenticating at {full_url}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Navigate to the sharing URL
        page.goto(full_url)

        # Wait for the password form to appear
        page.wait_for_selector('input[type="password"]', timeout=30000)

        # Enter password and submit
        page.fill('input[type="password"]', password)
        page.click('button[type="submit"]')

        # Wait for authentication to complete - look for file list or folder content
        # The page should redirect or show content after successful auth
        page.wait_for_selector(
            '[class*="file"], [class*="folder"], [class*="list"], [class*="grid"]',
            timeout=30000,
        )

        # Give it a moment for cookies to be set
        time.sleep(1)

        # Extract cookies
        cookies = context.cookies()
        cookie_dict = {}
        for cookie in cookies:
            if cookie["name"] in ("type", "sharing_sid"):
                cookie_dict[cookie["name"]] = cookie["value"]

        browser.close()

    if "sharing_sid" not in cookie_dict:
        raise RuntimeError(
            "Failed to obtain sharing_sid cookie. Authentication may have failed."
        )

    print("Authentication successful!")
    return cookie_dict


def make_api_request(
    base_url: str, cookies: dict, data: dict, timeout: int = 30
) -> dict:
    """
    Make POST request to the Synology API endpoint.

    Args:
        base_url: Base gofile URL
        cookies: Authentication cookies
        data: Form data to send
        timeout: Request timeout in seconds

    Returns:
        Parsed JSON response
    """
    endpoint = f"{base_url}/sharing/webapi/entry.cgi"
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}

    response = requests.post(
        endpoint, headers=headers, cookies=cookies, data=data, timeout=timeout
    )
    response.raise_for_status()
    return response.json()


def list_contents(
    base_url: str, cookies: dict, sharing_id: str, folder_path: str = "/"
) -> list[dict]:
    """
    List directory contents using the SYNO.FolderSharing.List API.

    Args:
        base_url: Base gofile URL
        cookies: Authentication cookies
        sharing_id: The sharing ID
        folder_path: Path to list (default: root)

    Returns:
        List of items with type (file/folder), name, size, etc.
    """
    data = {
        "api": "SYNO.FolderSharing.List",
        "method": "list",
        "version": "2",
        "offset": "0",
        "limit": "1000",
        "sort_by": '"name"',
        "sort_direction": '"ASC"',
        "action": '"enum"',
        "additional": '["size","owner","time","perm","type","mount_point_type"]',
        "filetype": '"all"',
        "folder_path": f'"{folder_path}"',
        "_sharing_id": f'"{sharing_id}"',
    }

    result = make_api_request(base_url, cookies, data)

    if not result.get("success"):
        error = result.get("error", {})
        raise RuntimeError(f"API error listing {folder_path}: {error}")

    return result.get("data", {}).get("items", [])


def build_download_url(
    base_url: str, sharing_id: str, file_path: str, filename: str
) -> str:
    """
    Build download URL with hex-encoded dlink parameter.

    Args:
        base_url: Base gofile URL
        sharing_id: The sharing ID
        file_path: Full path to the file
        filename: Name of the file

    Returns:
        Complete download URL
    """
    hex_path = file_path.encode("utf-8").hex()
    params = {
        "dlink": f'"{hex_path}"',
        "noCache": str(int(time.time() * 1000)),
        "_sharing_id": f'"{sharing_id}"',
        "api": "SYNO.FolderSharing.Download",
        "version": "2",
        "method": "download",
        "mode": "download",
        "stdhtml": "false",
    }
    encoded_filename = urllib.parse.quote(filename)
    query = urllib.parse.urlencode(params)
    return f"{base_url}/fsdownload/webapi/file_download.cgi/{encoded_filename}?{query}"


def download_file(
    base_url: str,
    cookies: dict,
    sharing_id: str,
    file_path: str,
    filename: str,
    output_path: Path,
    expected_size: int = 0,
    max_retries: int = 3,
) -> bool:
    """
    Download a single file with retry logic.

    Args:
        base_url: Base gofile URL
        cookies: Authentication cookies
        sharing_id: The sharing ID
        file_path: Full path to the file on the server
        filename: Name of the file
        output_path: Local path to save the file
        expected_size: Expected file size for verification
        max_retries: Maximum retry attempts

    Returns:
        True if download succeeded, False otherwise
    """
    download_url = build_download_url(base_url, sharing_id, file_path, filename)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    for attempt in range(max_retries):
        try:
            with requests.get(
                download_url, cookies=cookies, stream=True, timeout=30
            ) as r:
                r.raise_for_status()

                content_length = int(r.headers.get("content-length", 0))
                check_size = expected_size if expected_size > 0 else content_length

                # Stream download with progress
                downloaded = 0
                with open(temp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)

                # Verify size
                if check_size > 0 and downloaded != check_size:
                    raise IncompleteDownloadError(
                        f"Expected {check_size} bytes, got {downloaded}"
                    )

            # Success - move to final location
            shutil.move(str(temp_path), str(output_path))
            return True

        except (
            requests.RequestException,
            IncompleteDownloadError,
            OSError,
        ) as e:
            if attempt < max_retries - 1:
                wait_time = 2**attempt
                print(f"  Retry {attempt + 1}/{max_retries} for {filename}: {e}")
                time.sleep(wait_time)
            else:
                print(f"  Failed after {max_retries} attempts: {filename} - {e}")
                return False
        finally:
            # Clean up temp file on failure
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    return False


def get_unique_filename(output_dir: Path, filename: str) -> Path:
    """
    Get a unique filename, adding suffix if file already exists.

    Args:
        output_dir: Output directory
        filename: Original filename

    Returns:
        Unique path for the file
    """
    output_path = output_dir / filename
    if not output_path.exists():
        return output_path

    # Split name and extension
    stem = output_path.stem
    suffix = output_path.suffix

    counter = 1
    while True:
        new_name = f"{stem}_{counter}{suffix}"
        new_path = output_dir / new_name
        if not new_path.exists():
            return new_path
        counter += 1


def crawl_and_download(
    base_url: str,
    cookies: dict,
    sharing_id: str,
    output_dir: Path,
    root_path: str = "/",
    max_retries: int = 3,
    skip_existing: bool = False,
) -> dict:
    """
    Recursively traverse directories and download all files.

    Args:
        base_url: Base gofile URL
        cookies: Authentication cookies
        sharing_id: The sharing ID
        output_dir: Local directory to save files
        root_path: Starting path on the server
        max_retries: Maximum retry attempts per file
        skip_existing: Skip files that already exist locally

    Returns:
        Dictionary with counts: downloaded, failed, skipped
    """
    stats = {"downloaded": 0, "failed": 0, "skipped": 0}

    # Collect all files first
    all_files = []

    def collect_files(current_path: str):
        """Recursively collect all files."""
        try:
            items = list_contents(base_url, cookies, sharing_id, current_path)
        except Exception as e:
            print(f"Error listing {current_path}: {e}")
            return

        for item in items:
            name = item.get("name", "")
            is_folder = item.get("isdir", False)

            if current_path == "/":
                item_path = f"/{name}"
            else:
                item_path = f"{current_path}/{name}"

            if is_folder:
                collect_files(item_path)
            else:
                size = item.get("additional", {}).get("size", 0)
                all_files.append(
                    {"path": item_path, "name": name, "size": size}
                )

    print("Scanning directories...")
    collect_files(root_path)
    print(f"Found {len(all_files)} files to download")

    if not all_files:
        return stats

    # Download files with progress bar
    for file_info in tqdm(all_files, desc="Downloading", unit="file"):
        file_path = file_info["path"]
        filename = file_info["name"]
        size = file_info["size"]

        # Handle filename collisions
        output_path = get_unique_filename(output_dir, filename)

        # Check if we should skip
        if skip_existing and (output_dir / filename).exists():
            existing_size = (output_dir / filename).stat().st_size
            if existing_size == size:
                stats["skipped"] += 1
                continue

        success = download_file(
            base_url=base_url,
            cookies=cookies,
            sharing_id=sharing_id,
            file_path=file_path,
            filename=filename,
            output_path=output_path,
            expected_size=size,
            max_retries=max_retries,
        )

        if success:
            stats["downloaded"] += 1
        else:
            stats["failed"] += 1

    return stats


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Download photos from a Synology NAS gofile sharing link",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  %(prog)s --url https://gofile-xxx.quickconnect.to --sharing-id ZRnqeWks3 --password "yourpassword"
  %(prog)s --url https://gofile-xxx.quickconnect.to --sharing-id ZRnqeWks3 --password "yourpassword" --output ./photos --skip-existing
        """,
    )

    parser.add_argument(
        "--url",
        required=True,
        help="Base gofile URL (e.g., https://gofile-xxx.quickconnect.to)",
    )
    parser.add_argument(
        "--sharing-id",
        required=True,
        help="The sharing ID from URL path (e.g., ZRnqeWks3)",
    )
    parser.add_argument(
        "--password",
        required=True,
        help="Password for the shared folder",
    )
    parser.add_argument(
        "--output",
        default="./downloads",
        help="Output directory (default: ./downloads)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Max retry attempts per file (default: 3)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files that already exist locally with matching size",
    )

    args = parser.parse_args()

    # Normalize URL (remove trailing slash)
    base_url = args.url.rstrip("/")

    # Create output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output directory: {output_dir.absolute()}")

    # Authenticate
    try:
        cookies = authenticate(base_url, args.sharing_id, args.password)
    except Exception as e:
        print(f"Authentication failed: {e}")
        return 1

    # Crawl and download
    try:
        stats = crawl_and_download(
            base_url=base_url,
            cookies=cookies,
            sharing_id=args.sharing_id,
            output_dir=output_dir,
            max_retries=args.retries,
            skip_existing=args.skip_existing,
        )
    except Exception as e:
        print(f"Download failed: {e}")
        return 1

    # Report summary
    print("\n" + "=" * 50)
    print("Download Summary:")
    print(f"  Downloaded: {stats['downloaded']}")
    print(f"  Failed:     {stats['failed']}")
    print(f"  Skipped:    {stats['skipped']}")
    print("=" * 50)

    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    exit(main())
