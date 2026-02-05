# Synology NAS Photo Downloader

Downloads photos from a Synology NAS through gofile.me sharing links.

## Requirements

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Installation

```bash
# Install dependencies
uv pip install requests playwright tqdm

# Install Playwright browser
uv run playwright install chromium
```

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
| `--folder-path` | `/` | Starting folder path on the server |
| `--workers` | `4` | Number of concurrent download threads |
| `--retries` | `3` | Max retry attempts per file |
| `--skip-existing` | `false` | Skip files that already exist locally with matching size |
| `--debug` | `false` | Show browser window during authentication |

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
- Progress bars show download speed for each file
- Summary shows downloaded, failed, and skipped counts

## How It Works

1. Navigates to the gofile.me link using Playwright
2. Waits for redirect to the Synology quickconnect.to URL
3. Authenticates with the provided password
4. Recursively scans directories (with pagination for large folders)
5. Downloads files concurrently using multiple threads
