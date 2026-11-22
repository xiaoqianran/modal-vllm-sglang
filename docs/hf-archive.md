# Optional Hugging Face media archive

This CLI is **opt-in** and never modifies existing `outputs/` files. Task-level automatic delivery is described separately. No Modal/GPU calls are made by this CLI.

## Main flow

`ArchivePipeline.run()` executes: **prepare_media → provider.upload → provider.verify → describe → write_record**.

- `runtime/archive/pipeline.py`: readable main flow and replaceable provider interface
- `runtime/archive/operations.py`: local compression, hashing, and JSON record
- `runtime/archive/huggingface.py`: official HF Bucket API adapter
- `tools/archive_to_hf.py`: manual, standalone command

## Test and preview (no network)

```powershell
uv run --with pytest python -m pytest tests/test_archive_hf.py -q

uv run python -m tools.archive_to_hf "outputs/<task-name>/001_image.png" --bucket owner/public-bucket --project demo
```

The second command is a **dry run** unless `--upload` is supplied. Use a real media path.

## Upload explicitly

Create a **Public** Storage Bucket and configure your HF write token through a process secret/environment variable. Do not commit it.

```powershell
uv run python -m tools.archive_to_hf "outputs/<task-name>/001_image.png" --bucket owner/public-bucket --project demo --upload --verify sha256 --manifest archive-record.json
```

- `--compression none` disables recompression.
- `--compression adaptive_lossless` (default) tries lossless WebP for eligible 8-bit RGB/RGBA PNG or WebP files; retains the original unless at least 5% smaller.
- JPEG and MP4 are not recompressed.
- This CLI is manual (only `--upload` sends files); Task-level automatic delivery is configured separately. Existing `outputs/` files are never modified or deleted.
- `--verify size` checks remote size. `--verify sha256` downloads the uploaded object into a temporary directory to compare the full digest.
- A manifest is written **only when `--manifest` is provided**.
- This CLI only archives one file; Task-level automatic delivery has a separate CPU worker adapter. Neither path deletes Modal Volume sources.
- Uploaded paths incorporate the SHA256 of the stored bytes; uploading the same bytes again uses the same remote path. Concurrent/multiple writers and remote overwrite policies will require an additional integration review.

HF official API: https://huggingface.co/docs/huggingface_hub/guides/buckets

Automatic Task delivery (`local`, `huggingface`, `both`) is documented in [delivery-policy.md](delivery-policy.md).
