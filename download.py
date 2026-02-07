#!/usr/bin/env python3
"""
Synology NAS Photo Downloader

Downloads photos from a Synology NAS through the gofile sharing interface.
Uses Playwright for browser-based authentication and requests for file downloads.
"""

import argparse
import json
import shutil
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright
from tqdm import tqdm


class IncompleteDownloadError(Exception):
    """Raised when a download does not complete fully."""

    pass


class FailedDownloadLog:
    """Thread-safe logger for failed downloads. Writes JSON lines to a log file."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._lock = threading.Lock()

    def log_failure(self, file_info: dict, error: str):
        """Append a failed download entry to the log file."""
        entry = {
            "path": file_info["path"],
            "name": file_info["name"],
            "size": file_info["size"],
            "mtime": file_info["mtime"],
            "error": error,
            "timestamp": int(time.time()),
        }
        with self._lock:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")

    @staticmethod
    def read_failures(log_path: Path) -> list[dict]:
        """Read all failed download entries from a log file."""
        if not log_path.exists():
            return []
        entries = []
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        # Deduplicate by path (keep latest entry per path)
        seen = {}
        for entry in entries:
            seen[entry["path"]] = entry
        return list(seen.values())


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


def get_root_folder(base_url: str, cookies: dict, sharing_id: str) -> str:
    """
    Discover the actual root folder path via the SYNO.Core.Sharing.Initdata API.

    The sharing link's root is never literally "/". This API returns the real
    folder name (e.g. "09.26.25 Adrienne Dorley") which maps to "/{name}".

    Args:
        base_url: Base gofile URL
        cookies: Authentication cookies
        sharing_id: The sharing ID

    Returns:
        The root folder path (e.g. "/09.26.25 Adrienne Dorley")
    """
    data = {
        "api": "SYNO.Core.Sharing.Initdata",
        "method": "get",
        "version": "1",
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-SYNO-SHARING": sharing_id,
    }
    endpoint = f"{base_url}/sharing/webapi/entry.cgi"

    response = requests.post(
        endpoint, headers=headers, cookies=cookies, data=data, timeout=30
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("success"):
        raise RuntimeError(f"Failed to get sharing init data: {result.get('error', {})}")

    filename = result.get("data", {}).get("Private", {}).get("filename")
    if not filename:
        raise RuntimeError("Could not determine root folder name from Initdata response")

    root_path = f"/{filename}"
    print(f"Discovered root folder: {root_path}")
    return root_path


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
) -> tuple[bool, str]:
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
        Tuple of (success: bool, error_message: str). Error is empty on success.
    """
    download_url = build_download_url(base_url, sharing_id, file_path, filename)
    temp_path = output_path.with_suffix(output_path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
    last_error = ""

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
                        for chunk in r.iter_content(chunk_size=131072):
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
            return True, ""

        except (
            requests.RequestException,
            IncompleteDownloadError,
            OSError,
        ) as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                wait_time = 2**attempt
                print(f"  Retry {attempt + 1}/{max_retries} for {filename}: {e}")
                time.sleep(wait_time)
            else:
                print(f"  Failed after {max_retries} attempts: {filename} - {e}")
        finally:
            # Clean up temp file on failure
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    return False, last_error


class UniqueFilenameAllocator:
    """Thread-safe filename allocator that prevents collisions between concurrent downloads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._reserved: set[Path] = set()

    def allocate(self, output_dir: Path, filename: str) -> Path:
        """
        Get a unique filename, considering both existing files on disk
        and names already reserved by other threads.

        Args:
            output_dir: Output directory
            filename: Original filename

        Returns:
            Unique path for the file (reserved until released)
        """
        with self._lock:
            output_path = output_dir / filename
            if not output_path.exists() and output_path not in self._reserved:
                self._reserved.add(output_path)
                return output_path

            stem = output_path.stem
            suffix = output_path.suffix

            counter = 1
            while True:
                new_name = f"{stem}_{counter}{suffix}"
                new_path = output_dir / new_name
                if not new_path.exists() and new_path not in self._reserved:
                    self._reserved.add(new_path)
                    return new_path
                counter += 1

    def release(self, path: Path):
        """Release a reserved filename (call after download completes or fails)."""
        with self._lock:
            self._reserved.discard(path)


def crawl_and_download(
    base_url: str,
    cookies: dict,
    sharing_id: str,
    output_dir: Path,
    root_path: str = "/",
    max_retries: int = 3,
    skip_existing: bool = False,
    workers: int = 4,
    failed_log: FailedDownloadLog | None = None,
) -> dict:
    """
    Recursively traverse directories and download all .CR3 files.

    Args:
        base_url: Base gofile URL
        cookies: Authentication cookies
        sharing_id: The sharing ID
        output_dir: Local directory to save files
        root_path: Starting path on the server
        max_retries: Maximum retry attempts per file
        skip_existing: Skip files that already exist locally
        workers: Number of concurrent download threads
        failed_log: Logger for failed downloads

    Returns:
        Dictionary with counts: downloaded, failed, skipped, filtered
    """
    stats = {"downloaded": 0, "failed": 0, "skipped": 0, "filtered": 0}

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
                # Only collect .CR3 files
                if not name.upper().endswith(".CR3"):
                    stats["filtered"] += 1
                    continue

                additional = item.get("additional", {})
                size = additional.get("size", 0)
                mtime = additional.get("time", {}).get("mtime", int(time.time()))
                all_files.append(
                    {"path": item_path, "name": name, "size": size, "mtime": mtime}
                )

    print("Scanning directories...")
    collect_files(root_path)
    print(f"Found {len(all_files)} .CR3 files to download (filtered {stats['filtered']} non-CR3 files)")

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

    allocator = UniqueFilenameAllocator()

    def download_task(file_info):
        """Download a single file (for thread pool)."""
        file_path = file_info["path"]
        filename = file_info["name"]
        size = file_info["size"]
        mtime = file_info["mtime"]

        # Use epoch timestamp as filename, preserve extension
        ext = Path(filename).suffix
        epoch_filename = f"{mtime}{ext}"
        output_path = allocator.allocate(output_dir, epoch_filename)

        try:
            success, error = download_file(
                base_url=base_url,
                cookies=cookies,
                sharing_id=sharing_id,
                file_path=file_path,
                filename=filename,
                output_path=output_path,
                expected_size=size,
                max_retries=max_retries,
            )
        except Exception as e:
            success, error = False, str(e)
        finally:
            allocator.release(output_path)
        return success, error, file_info

    # Download files concurrently with progress bar
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(download_task, f): f for f in files_to_download}
        with tqdm(total=len(files_to_download), desc="Downloading", unit="file") as pbar:
            for future in as_completed(futures):
                success, error, file_info = future.result()
                if success:
                    stats["downloaded"] += 1
                else:
                    stats["failed"] += 1
                    if failed_log:
                        failed_log.log_failure(file_info, error)
                pbar.update(1)

    return stats


def retry_failed_downloads(
    base_url: str,
    cookies: dict,
    sharing_id: str,
    output_dir: Path,
    failed_log_path: Path,
    max_retries: int = 3,
    workers: int = 4,
) -> dict:
    """
    Retry downloading only the files that previously failed.

    Reads entries from the failed downloads log and attempts to download them again.
    On success, the entry is removed from the log. Persistent failures are re-logged.

    Args:
        base_url: Base gofile URL
        cookies: Authentication cookies
        sharing_id: The sharing ID
        output_dir: Local directory to save files
        failed_log_path: Path to the failed downloads log
        max_retries: Maximum retry attempts per file
        workers: Number of concurrent download threads

    Returns:
        Dictionary with counts: downloaded, failed
    """
    stats = {"downloaded": 0, "failed": 0}

    failed_files = FailedDownloadLog.read_failures(failed_log_path)
    if not failed_files:
        print("No failed downloads to retry.")
        return stats

    print(f"Retrying {len(failed_files)} previously failed downloads...")

    # Clear the old log - we'll re-log anything that still fails
    failed_log_path.unlink()
    new_log = FailedDownloadLog(failed_log_path)

    allocator = UniqueFilenameAllocator()

    def download_task(file_info):
        file_path = file_info["path"]
        filename = file_info["name"]
        size = file_info["size"]
        mtime = file_info["mtime"]

        ext = Path(filename).suffix
        epoch_filename = f"{mtime}{ext}"
        output_path = allocator.allocate(output_dir, epoch_filename)

        try:
            success, error = download_file(
                base_url=base_url,
                cookies=cookies,
                sharing_id=sharing_id,
                file_path=file_path,
                filename=filename,
                output_path=output_path,
                expected_size=size,
                max_retries=max_retries,
            )
        except Exception as e:
            success, error = False, str(e)
        finally:
            allocator.release(output_path)
        return success, error, file_info

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(download_task, f): f for f in failed_files}
        with tqdm(total=len(failed_files), desc="Retrying", unit="file") as pbar:
            for future in as_completed(futures):
                success, error, file_info = future.result()
                if success:
                    stats["downloaded"] += 1
                else:
                    stats["failed"] += 1
                    new_log.log_failure(file_info, error)
                pbar.update(1)

    return stats


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Download .CR3 photos from a Synology NAS gofile sharing link",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  %(prog)s --link https://gofile.me/7g4WA/ZRnqeWks3 --password "yourpassword"
  %(prog)s --link https://gofile.me/7g4WA/ZRnqeWks3 --password "yourpassword" --output ./photos --skip-existing
  %(prog)s --link https://gofile.me/7g4WA/ZRnqeWks3 --password "yourpassword" --retry-failed
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
        default=None,
        help="Starting folder path on the server (auto-detected if not specified)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of concurrent download threads (default: 4)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Only retry previously failed downloads from failed_downloads.log",
    )

    args = parser.parse_args()

    # Create output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    failed_log_path = output_dir / "failed_downloads.log"

    print(f"Output directory: {output_dir.absolute()}")

    # Authenticate (this also resolves the redirect and extracts base_url/sharing_id)
    try:
        base_url, sharing_id, cookies = authenticate(
            args.link, args.password, headless=not args.debug
        )
    except Exception as e:
        print(f"Authentication failed: {e}")
        return 1

    # Discover root folder path if not explicitly provided
    if args.folder_path is None and not args.retry_failed:
        try:
            root_path = get_root_folder(base_url, cookies, sharing_id)
        except Exception as e:
            print(f"Failed to auto-detect root folder: {e}")
            print("Try specifying --folder-path manually.")
            return 1
    else:
        root_path = args.folder_path or "/"

    if args.retry_failed:
        # Retry only previously failed downloads
        try:
            stats = retry_failed_downloads(
                base_url=base_url,
                cookies=cookies,
                sharing_id=sharing_id,
                output_dir=output_dir,
                failed_log_path=failed_log_path,
                max_retries=args.retries,
                workers=args.workers,
            )
        except Exception as e:
            print(f"Retry failed: {e}")
            return 1
    else:
        # Normal crawl and download
        failed_log = FailedDownloadLog(failed_log_path)
        try:
            stats = crawl_and_download(
                base_url=base_url,
                cookies=cookies,
                sharing_id=sharing_id,
                output_dir=output_dir,
                root_path=root_path,
                max_retries=args.retries,
                skip_existing=args.skip_existing,
                workers=args.workers,
                failed_log=failed_log,
            )
        except Exception as e:
            print(f"Download failed: {e}")
            return 1

    # Report summary
    print("\n" + "=" * 50)
    print("Download Summary:")
    print(f"  Downloaded: {stats['downloaded']}")
    print(f"  Failed:     {stats['failed']}")
    if "skipped" in stats:
        print(f"  Skipped:    {stats['skipped']}")
    if stats.get("filtered", 0) > 0:
        print(f"  Filtered:   {stats['filtered']} (non-CR3 files)")
    print("=" * 50)

    if stats["failed"] > 0:
        print(f"\nFailed downloads logged to: {failed_log_path}")
        print("Re-run with --retry-failed to retry only those files.")

    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    exit(main())
