# Optional Hugging Face media archive

This is an **opt-in** feature. The existing `orchestration` worker and `outputs/` download behavior remain unchanged. No Modal/GPU calls are made by this CLI.

## Main flow

`ArchivePipeline.run()` executes: **prepare_media → provider.upload → provider.verify → describe → write_record**.

- `runtime/archive/pipeline.py`: readable main flow and replaceable provider interface
- `runtime/archive/operations.py`: local compression, hashing, and JSON record
- `runtime/archive/huggingface.py`: official HF Bucket API adapter
- `tools/archive_to_hf.py`: manual, standalone command

## Test and preview (no network)

```powershell
uv run --with pillow --with pytest --with huggingface_hub python -m pytest tests/test_archive_hf.py -q

uv run --with pillow --with huggingface_hub python -m tools.archive_to_hf "outputs/<project>/<scene>/001.png" --bucket owner/public-bucket --project demo
```

The second command is a **dry run** unless `--upload` is supplied. Use a real media path.

## Upload explicitly

Create a **Public** Storage Bucket and configure your HF write token through a process secret/environment variable. Do not commit it.

```powershell
uv run --with pillow --with huggingface_hub python -m tools.archive_to_hf "outputs/<project>/<scene>/001.png" --bucket owner/public-bucket --project demo --upload --verify sha256 --manifest archive-record.json
```

- `--compression none` disables recompression.
- `--compression adaptive_lossless` (default) tries lossless WebP for eligible 8-bit RGB/RGBA PNG or WebP files; retains the original unless at least 5% smaller.
- JPEG and MP4 are not recompressed.
- Upload is **not automatic**; current `outputs/` files are **never deleted or changed**.
- `--verify size` checks remote size. `--verify sha256` downloads the uploaded object into a temporary directory to compare the full digest.
- A manifest is written **only when `--manifest` is provided**.
- This is a single-file archive utility, not a replacement for automatic workflow collection. No cleanup, Volume deletion, or retries are enabled.
- Uploaded paths incorporate the SHA256 of the stored bytes; uploading the same bytes again uses the same remote path. Concurrent/multiple writers and remote overwrite policies will require an additional integration review.

HF official API: https://huggingface.co/docs/huggingface_hub/guides/buckets
