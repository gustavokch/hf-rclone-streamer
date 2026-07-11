# HF to GDrive Streamer

Stream Hugging Face model downloads to Google Drive via rclone, using localhost as a VFS cache layer. Perfect for downloading large models (70GB+) without permanent local storage, with resume capability and aria2c-powered high-speed downloads.

## Features

- **High-speed downloads**: Uses aria2c with multi-connection downloads (16 connections by default)
- **Streaming architecture**: Downloads directly to temp cache, then streams to Google Drive
- **Resume capability**: Interrupted transfers can be resumed with `--resume`
- **Flexible rclone support**: Works with both mounted drives and config files
- **Progress tracking**: Real-time progress bars for download and upload
- **State checkpointing**: Transfer state saved for recovery after crashes
- **Batch downloads**: Download multiple models from a file
- **Search integration**: Search Hugging Face Hub and download interactively

## Prerequisites

### System Requirements

1. **Python 3.9+**
2. **aria2c** - Required for high-speed downloads
   ```bash
   # macOS
   brew install aria2

   # Ubuntu/Debian
   sudo apt install aria2

   # Fedora
   sudo dnf install aria2
   ```

3. **rclone** - Required for Google Drive operations
   ```bash
   # macOS
   brew install rclone

   # Ubuntu/Debian
   sudo apt install rclone

   # Or download from: https://rclone.org/downloads/
   ```

4. **Google Drive Setup**: Configure rclone with Google Drive
   ```bash
   rclone config
   # Follow prompts to set up a Google Drive remote
   ```

### Optional: Mount Google Drive

For better performance, mount Google Drive locally:

```bash
# Create mount point
mkdir -p ~/gdrive

# Mount (runs in foreground)
rclone mount gdrive: ~/gdrive --vfs-cache-mode full

# Or mount in background with logging
rclone mount gdrive: ~/gdrive --vfs-cache-mode full --daemon --log-file /tmp/rclone.log
```

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/yourusername/huggingface-rclone-streamer.git
   cd huggingface-rclone-streamer
   ```

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. (Optional) Install as a package:
   ```bash
   pip install -e .
   ```

## Configuration

Configuration can be provided via:
- Command-line arguments
- Environment variables (prefix: `HF_RCLONE_`)
- Config file (`~/.config/hf-rclone-streamer/config.json`)

### Environment Variables

```bash
export HF_RCLONE_RCLONE_PATH=~/gdrive          # Mount or config path
export HF_RCLONE_DEST_DIR=/Models              # GDrive destination
export HF_RCLONE_CACHE_DIR=/tmp/hf_cache       # Local cache
export HF_RCLONE_MAX_SHARD_SIZE=5368709120     # 5GB
export HF_RCLONE_CLEANUP=true                  # Delete cache after upload
export HF_RCLONE_RESUME=false                  # Resume capability
export HF_RCLONE_MAX_RETRIES=3                 # Retry attempts
export HF_RCLONE_HF_TOKEN=your_token           # HF auth token
export HF_RCLONE_ARIA2C_CONNECTIONS=16         # aria2c connections
```

### Config File Example

```json
{
  "rclone_path": "~/gdrive",
  "dest_dir": "/Models",
  "cache_dir": "/tmp/hf_cache",
  "max_shard_size": 5368709120,
  "cleanup": true,
  "resume": false,
  "aria2c_connections": 16
}
```

## Usage

### Basic Download

Download a model to your Google Drive mount:

```bash
python hf_rclone_streamer.py download meta-llama/Llama-3.1-8B \
    --rclone-path ~/gdrive
```

### Using rclone Config (No Mount)

If you don't have a mounted drive:

```bash
python hf_rclone_streamer.py download meta-llama/Llama-3.1-8B \
    --rclone-path ~/.config/rclone/rclone.conf
```

### Search and Download

Search for models and interactively select one to download:

```bash
# Search only
python hf_rclone_streamer.py search "llama 3" --author meta-llama

# Search and download interactively
python hf_rclone_streamer.py search "llama 3" --download
```

### Resume Interrupted Transfer

If a transfer was interrupted:

```bash
python hf_rclone_streamer.py download meta-llama/Llama-3.1-8B \
    --rclone-path ~/gdrive --resume
```

### High-Speed Downloads

Increase aria2c connections for faster downloads:

```bash
python hf_rclone_streamer.py download meta-llama/Llama-3.1-8B \
    --aria2c-connections 32
```

### Batch Download

Download multiple models from a file:

```bash
# Create models.txt with one model ID per line:
# meta-llama/Llama-3.1-8B
# microsoft/phi-2
# google/gemma-7b

python hf_rclone_streamer.py batch models.txt --rclone-path ~/gdrive
```

### Filter Files

Download only specific file types:

```bash
# Only safetensors files
python hf_rclone_streamer.py download meta-llama/Llama-3.1-8B \
    --include "*.safetensors" --rclone-path ~/gdrive

# Exclude README files
python hf_rclone_streamer.py download meta-llama/Llama-3.1-8B \
    --exclude "README*" --exclude "*.md" --rclone-path ~/gdrive
```

### Model Information

View information about a model before downloading:

```bash
python hf_rclone_streamer.py info meta-llama/Llama-3.1-8B
```

### Check Transfer Status

View status of current/resumable transfers:

```bash
python hf_rclone_streamer.py status
```

## Command Reference

### Global Options

| Option | Description | Default |
|--------|-------------|---------|
| `--rclone-path` | Path to rclone mount or config | Auto-detect |
| `--cache-dir` | Temporary cache directory | `/tmp/hf_cache` |
| `--dest-dir` | Destination on GDrive | `/Models` |
| `--include` | Glob pattern for files to include | `*` |
| `--exclude` | Glob patterns to exclude | None |
| `--no-cleanup` | Don't delete cache after upload | False |
| `--resume` | Resume from existing cache | False |
| `--hf-token` | Hugging Face auth token | None |
| `--no-aria2c` | Disable aria2c | False |
| `--aria2c-connections` | aria2c connections per file | 16 |

### Commands

#### `download <model_id>`
Download a model from Hugging Face.

#### `search <query>`
Search for models on Hugging Face Hub.

#### `batch <file>`
Download multiple models from a file.

#### `info <model_id>`
Show information about a model.

#### `status`
Show transfer status and history.

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Hugging Face   │────▶│  Local Cache    │────▶│  Google Drive   │
│     Hub         │     │  (VFS/Temp)     │     │  (via rclone)   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                               ▲
                               │
                         aria2c (16 conn)
```

1. **Discovery**: Query Hugging Face Hub API for model metadata
2. **Download**: aria2c downloads files to `/tmp/hf_cache` (or custom cache dir)
3. **Cache**: Files are cached locally for resume capability
4. **Upload**: Files are streamed to Google Drive via rclone
5. **Cleanup**: Optional cache cleanup after successful upload

## Troubleshooting

### "aria2c not found"

Install aria2c:
```bash
# macOS
brew install aria2

# Ubuntu/Debian
sudo apt install aria2
```

Or disable aria2c:
```bash
python hf_rclone_streamer.py download ... --no-aria2c
```

### "rclone not found"

Install rclone:
```bash
# macOS
brew install rclone

# Or download from: https://rclone.org/downloads/
```

### "Mount not accessible"

Check if mount point exists:
```bash
ls ~/gdrive

# Remount if needed
rclone mount gdrive: ~/gdrive --vfs-cache-mode full
```

### Transfer interrupted

Use `--resume` to continue:
```bash
python hf_rclone_streamer.py download ... --resume
```

### Out of space on Google Drive

Check available space:
```bash
rclone about gdrive:
```

Delete old models or use a different account.

### Slow downloads

Increase aria2c connections:
```bash
--aria2c-connections 32
```

Or check network connectivity and HF rate limits.

## Examples

### Download Llama 3.1 8B

```bash
python hf_rclone_streamer.py download meta-llama/Llama-3.1-8B \
    --rclone-path ~/gdrive \
    --dest-dir "/Models/Llama"
```

### Download with custom cache

```bash
python hf_rclone_streamer.py download microsoft/phi-2 \
    --rclone-path ~/gdrive \
    --cache-dir ~/hf_cache \
    --resume
```

### Search and download small models

```bash
python hf_rclone_streamer.py search "gpt2" --download
```

### Batch download curated list

```bash
cat > models.txt << EOF
# Language Models
gpt2
microsoft/phi-2
google/gemma-2b

# Embedding Models
sentence-transformers/all-MiniLM-L6-v2
EOF

python hf_rclone_streamer.py batch models.txt --rclone-path ~/gdrive
```

## License

MIT License - feel free to use and modify as needed.

## Contributing

Contributions welcome! Please feel free to submit pull requests or open issues.
