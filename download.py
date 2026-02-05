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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright
from tqdm import tqdm


class IncompleteDownloadError(Exception):
    """Raised when a download does not complete fully."""

    pass


def authenticate(link: str, password: str, headless: bool = True) -> tuple[str, str, dict[str, str]]:
    """
    Use Playwright to authenticate with a gofile.me sharing URL.

    Navigates to the gofile.me link, waits for redirect to quickconnect.to,
    then authenticates with the password.

    Args:
        link: gofile.me sharing URL (e.g., https://gofile.me/7g4WA/ZRnqeWks3)
        password: Password for the shared folder
        headless: Run browser in headless mode (default: True)

    Returns:
        Tuple of (base_url, sharing_id, cookies_dict)
    """
    print(f"Navigating to {link}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        # Navigate to the gofile.me URL
        page.goto(link)

        # Wait for redirect to quickconnect.to
        print("Waiting for redirect to quickconnect.to...")
        page.wait_for_url(lambda url: "quickconnect.to" in url, timeout=60000)

        # Extract base URL and sharing ID from the redirected URL
        current_url = page.url
        print(f"Redirected to: {current_url}")

        parsed = urllib.parse.urlparse(current_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        # Extract sharing_id from path (e.g., /sharing/ZRnqeWks3)
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 2 and path_parts[0] == "sharing":
            sharing_id = path_parts[1]
        else:
            raise RuntimeError(f"Could not extract sharing_id from URL: {current_url}")

        print(f"Base URL: {base_url}")
        print(f"Sharing ID: {sharing_id}")

        # Wait for the password form to appear
        print("Waiting for password form...")
        page.wait_for_selector('input[name="ext-comp-1025"]', timeout=30000)

        print("Password form loaded.")
        # Enter password and submit
        page.fill('input[name="ext-comp-1025"]', password)
        page.press('input[name="ext-comp-1025"]', 'Enter')

        # Wait for authentication to complete - look for file list or folder content
        # The page should redirect or show content after successful auth
#        page.wait_for_selector(
#            '[class*="file"], [class*="folder"], [class*="list"], [class*="grid"]',
#            timeout=30000,
#        )

        # Set up network request logging to capture API calls
        api_requests = []
        def log_request(request):
            if "webapi" in request.url:
                api_requests.append({
                    "url": request.url,
                    "method": request.method,
                    "post_data": request.post_data,
                })
        page.on("request", log_request)

        # Poll for sharing_sid cookie for up to 30 seconds
        print("Waiting for sharing_sid cookie...")
        cookie_dict = {}
        max_wait = 30
        start_time = time.time()

        while time.time() - start_time < max_wait:
            cookies = context.cookies()
            elapsed = time.time() - start_time
            print(f"  [{elapsed:.1f}s] Found cookies: {[(c['name'], c['domain']) for c in cookies]}")

            # Capture all cookies (not just specific ones)
            for cookie in cookies:
                cookie_dict[cookie["name"]] = cookie["value"]

            if "sharing_sid" in cookie_dict:
                print(f"  Got sharing_sid cookie after {elapsed:.1f}s")
                # Wait for the file list to load (this triggers the real API call)
                print("Waiting for file list to load...")
                try:
                    page.wait_for_selector('[class*="x-grid"], [class*="thumb"], .x-panel', timeout=15000)
                    time.sleep(2)  # Give it time to complete API requests
                except Exception:
                    time.sleep(5)  # Fallback wait
                break

            time.sleep(0.5)
        else:
            print(f"  Timed out after {max_wait}s waiting for sharing_sid cookie")

        # Print captured API requests
        if api_requests:
            print("\nCaptured API requests from browser:")
            for req in api_requests:
                print(f"  URL: {req['url']}")
                print(f"  Method: {req['method']}")
                print(f"  Post data: {req['post_data']}")
                print()

        browser.close()

    if "sharing_sid" not in cookie_dict:
        raise RuntimeError(
            "Failed to obtain sharing_sid cookie. Authentication may have failed."
        )

    print("Authentication successful!")
    print(f"Captured cookies: {list(cookie_dict.keys())}")
    return base_url, sharing_id, cookie_dict


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
    Handles pagination to fetch all items.

    Args:
        base_url: Base gofile URL
        cookies: Authentication cookies
        sharing_id: The sharing ID
        folder_path: Path to list (default: root)

    Returns:
        List of items with type (file/folder), name, size, etc.
    """
    all_items = []
    offset = 0
    limit = 1000
    total = None

    while True:
        data = {
            "api": "SYNO.FolderSharing.List",
            "method": "list",
            "version": "2",
            "offset": str(offset),
            "limit": str(limit),
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

        data_obj = result.get("data", {})
        items = data_obj.get("files", [])
        total = data_obj.get("total", 0)

        all_items.extend(items)

        # Log progress for directories with many items
        if total > limit:
            print(f"  [{folder_path}] Fetched {len(all_items)}/{total} items...")

        # Check if we have all items
        if len(all_items) >= total or len(items) < limit:
            break

        offset += limit

    return all_items


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
                download_url, cookies=cookies, stream=True, timeout=(10, 300)
            ) as r:
                r.raise_for_status()

                content_length = int(r.headers.get("content-length", 0))
                check_size = expected_size if expected_size > 0 else content_length

                # Stream download with progress bar showing speed
                downloaded = 0
                with open(temp_path, "wb") as f:
                    with tqdm(
                        total=check_size,
                        unit="B",
                        unit_scale=True,
                        unit_divisor=1024,
                        desc=filename[:30],
                        leave=False,
                    ) as pbar:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                pbar.update(len(chunk))

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
    workers: int = 4,
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
        workers: Number of concurrent download threads

    Returns:
        Dictionary with counts: downloaded, failed, skipped
    """
    stats = {"downloaded": 0, "failed": 0, "skipped": 0}

    # Collect all files first
    all_files = []

    def collect_files(current_path: str):
        """Recursively collect all files."""
        print(f"Scanning: {current_path}")
        try:
            items = list_contents(base_url, cookies, sharing_id, current_path)
            print(f"  Found {len(items)} items in {current_path}")
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
                additional = item.get("additional", {})
                size = additional.get("size", 0)
                mtime = additional.get("time", {}).get("mtime", int(time.time()))
                all_files.append(
                    {"path": item_path, "name": name, "size": size, "mtime": mtime}
                )

    print("Scanning directories...")
    collect_files(root_path)
    print(f"Found {len(all_files)} files to download")

    if not all_files:
        return stats

    # Filter out files to skip
    files_to_download = []
    for file_info in all_files:
        filename = file_info["name"]
        size = file_info["size"]
        if skip_existing and (output_dir / filename).exists():
            existing_size = (output_dir / filename).stat().st_size
            if existing_size == size:
                stats["skipped"] += 1
                continue
        files_to_download.append(file_info)

    if not files_to_download:
        print(f"Skipped {stats['skipped']} existing files, nothing to download")
        return stats

    print(f"Downloading {len(files_to_download)} files with {workers} threads...")

    def download_task(file_info):
        """Download a single file (for thread pool)."""
        file_path = file_info["path"]
        filename = file_info["name"]
        size = file_info["size"]
        mtime = file_info["mtime"]

        # Use epoch timestamp as filename, preserve extension
        ext = Path(filename).suffix
        epoch_filename = f"{mtime}{ext}"
        output_path = get_unique_filename(output_dir, epoch_filename)

        return download_file(
            base_url=base_url,
            cookies=cookies,
            sharing_id=sharing_id,
            file_path=file_path,
            filename=filename,
            output_path=output_path,
            expected_size=size,
            max_retries=max_retries,
        )

    # Download files concurrently with progress bar
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(download_task, f): f for f in files_to_download}
        with tqdm(total=len(files_to_download), desc="Downloading", unit="file") as pbar:
            for future in as_completed(futures):
                if future.result():
                    stats["downloaded"] += 1
                else:
                    stats["failed"] += 1
                pbar.update(1)

    return stats


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Download photos from a Synology NAS gofile sharing link",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  %(prog)s --link https://gofile.me/7g4WA/ZRnqeWks3 --password "yourpassword"
  %(prog)s --link https://gofile.me/7g4WA/ZRnqeWks3 --password "yourpassword" --output ./photos --skip-existing
        """,
    )

    parser.add_argument(
        "--link",
        required=True,
        help="gofile.me sharing URL (e.g., https://gofile.me/7g4WA/ZRnqeWks3)",
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
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show browser window during authentication (non-headless mode)",
    )
    parser.add_argument(
        "--folder-path",
        default="/",
        help="Starting folder path on the server (default: /)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of concurrent download threads (default: 4)",
    )

    args = parser.parse_args()

    # Create output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output directory: {output_dir.absolute()}")

    # Authenticate (this also resolves the redirect and extracts base_url/sharing_id)
    try:
        base_url, sharing_id, cookies = authenticate(
            args.link, args.password, headless=not args.debug
        )
    except Exception as e:
        print(f"Authentication failed: {e}")
        return 1

    # Crawl and download
    try:
        stats = crawl_and_download(
            base_url=base_url,
            cookies=cookies,
            sharing_id=sharing_id,
            output_dir=output_dir,
            root_path=args.folder_path,
            max_retries=args.retries,
            skip_existing=args.skip_existing,
            workers=args.workers,
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
