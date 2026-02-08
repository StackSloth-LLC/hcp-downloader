# Synology NAS Photo Downloader

Downloads .CR3 photos from a Synology NAS through gofile.me sharing links.

## Features

- **Browser-based authentication** - Uses Playwright to handle the gofile.me -> quickconnect.to redirect and password login
- **Auto-detection of root folder** - Automatically discovers the shared folder path via the Synology Initdata API
- **Recursive directory scanning** - Traverses all subdirectories to find files
- **CR3 file filtering** - Only downloads `.CR3` (Canon RAW) files, skipping JPG, MP4, and other formats
- **Concurrent downloads** - Configurable number of parallel download threads
- **Pagination support** - Handles directories with thousands of files (fetches in batches of 1000)
- **Resume support** - Skip files that already exist locally with matching size
- **Failed download logging** - Logs failures to a JSON-lines file for targeted retry
- **Retry failed downloads** - Re-run with `--retry-failed` to retry only previously failed files without re-scanning
- **Automatic retries** - Configurable per-file retry count with exponential backoff
- **Download verification** - Validates downloaded file size against the expected size
- **Atomic downloads** - Files are downloaded to a temp file and moved into place on success
- **Epoch-based filenames** - Files are saved using their modification timestamp (e.g., `1758941669.CR3`)
- **Collision-safe filenames** - Thread-safe deduplication adds suffixes when timestamps collide (e.g., `1758941669_1.CR3`)
- **Per-file progress bars** - Shows download speed and progress for each file
- **Overall progress bar** - Shows total file count progress across all downloads

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager

## Installation

### Install uv

uv is a fast Python package and project manager. Install it with one command:

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

After installing, restart your terminal or run `source $HOME/.local/bin/env` so that `uv` is on your PATH.

### Install project dependencies

```bash
# Clone the repo and cd into it, then:
uv sync

# Install the Playwright Chromium browser
uv run playwright install chromium
```

`uv sync` reads `pyproject.toml`, creates a virtual environment, and installs all dependencies (requests, playwright, tqdm) automatically.

## Usage

```bash
uv run download.py --link <GOFILE_LINK> --password <PASSWORD> [OPTIONS]
```

### Required Arguments

| Argument | Description |
|----------|-------------|
| `--link` | gofile.me sharing URL (e.g., `https://gofile.me/7g4WA/ZRnqeWks3`) |
| `--password` | Password for the shared folder |

### Optional Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--output` | `./downloads` | Output directory for downloaded files |
| `--folder-path` | auto-detect | Starting folder path on the server. If omitted, the root folder is auto-discovered via the Synology API |
| `--workers` | `4` | Number of concurrent download threads |
| `--retries` | `3` | Max retry attempts per file (uses exponential backoff) |
| `--skip-existing` | off | Skip files that already exist locally with matching size |
| `--retry-failed` | off | Skip directory scanning and only retry files from `failed_downloads.log` |
| `--debug` | off | Show browser window during authentication (non-headless mode) |

## Examples

### Download all files from a share

```bash
uv run download.py \
  --link "https://gofile.me/7g4WA/ZRnqeWks3" \
  --password "mypassword" \
  --output ~/Photos
```

### Download from a specific folder with 8 threads

```bash
uv run download.py \
  --link "https://gofile.me/7g4WA/ZRnqeWks3" \
  --password "mypassword" \
  --folder-path "/2024 Photos/Summer" \
  --output ~/Photos \
  --workers 8
```

### Resume a download (skip existing files)

```bash
uv run download.py \
  --link "https://gofile.me/7g4WA/ZRnqeWks3" \
  --password "mypassword" \
  --output ~/Photos \
  --skip-existing
```

### Retry only previously failed downloads

```bash
uv run download.py \
  --link "https://gofile.me/7g4WA/ZRnqeWks3" \
  --password "mypassword" \
  --output ~/Photos \
  --retry-failed
```

### Debug authentication issues

```bash
uv run download.py \
  --link "https://gofile.me/7g4WA/ZRnqeWks3" \
  --password "mypassword" \
  --debug
```

## Output

- Files are saved with their original modification timestamp as the filename (e.g., `1758941669.CR3`)
- If multiple files have the same timestamp, a suffix is added (e.g., `1758941669_1.CR3`)
- Only `.CR3` files are downloaded; all other file types are filtered out
- Per-file progress bars show download speed
- An overall progress bar tracks total file completion
- A summary at the end shows downloaded, failed, skipped, and filtered counts
- Failed downloads are logged to `<output_dir>/failed_downloads.log` (JSON lines format)

## How It Works

1. Navigates to the gofile.me link using Playwright
2. Waits for redirect to the Synology quickconnect.to URL
3. Authenticates with the provided password and extracts the `sharing_sid` cookie
4. Auto-discovers the root folder path via `SYNO.Core.Sharing.Initdata` API
5. Recursively scans directories with pagination via `SYNO.FolderSharing.List` API
6. Downloads `.CR3` files concurrently using multiple threads
7. Verifies file sizes and retries failures with exponential backoff
8. Logs any persistent failures for later retry with `--retry-failed`
