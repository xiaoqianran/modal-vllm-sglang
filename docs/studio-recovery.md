# Studio archive recovery and media delivery

## Immutable public recovery points

`python -m tools.studio_manifest export --output outputs/.runtime/studio-manifest-v1.json`

Add `--publish` only when the sanitized project IDs, media filenames, sizes and
content hashes may be public. The manifest never contains prompts, credentials,
local machine paths or executable task definitions. Publication uses the existing
SHA-256 readback contract and uploads metadata only. No media is re-uploaded.

Discover recovery points from a clean checkout:

```
python -m tools.studio_manifest list
python -m tools.studio_manifest restore --remote manifests/studio/v1/<prefix>/<digest>.json --output recovery
```

The destination must be empty. The built Studio template must be available in the
checkout. The restored HTML is read-only and does not claim to restore runner
state, task history or prompts. Keep the recovery point address externally as a
backup; listing is not a chronological latest-pointer mechanism. Asset receipts
are locally verified records, not a fresh remote byte audit of every asset.

The export refuses to publish if any gallery entry lacks a matching SHA-256
receipt. Legacy project-scoped paths are supported when their filenames match
stored content hashes. The manifest digest protects its own bytes; it does not
provide a signature or authorization against a malicious bucket writer.

## Media policy

- Images: retain the archival master (exact bytes if required) and use the
  full-resolution high-quality preview as the gallery default. Never replace it
  with a smaller local-only derivative. Benchmark AVIF versus WebP at comparable
  quality, not identical numeric encoder quality. Use original pixels for zoom
  and download when requested.
- Videos: preserve the original MP4 byte-for-byte. Studio plays the original
  URL by default, on user interaction only. Generate a small WebP poster for
  browsing; never implicitly switch playback to a lower resolution. Optional
  native-resolution H.264 fast-start copies are experimental, not production
  substitutes. Require matching dimensions, frame count, frame rate, audio
  stream count and duration (within 50ms), VMAF >= 97, SSIM >= 0.98, and at
  least 20% byte savings before even considering an opt-in derivative. These
  metrics are screening thresholds, not a guarantee of perceptual quality.
  Preserve the original even if a derivative passes. Small original H.264
  clips need no re-encoding. Benchmark lower-resolution variants only as a
  comparison, never as default playback.
- Derivatives should have immutable content addresses and source digest + encoder
  policy indexes. Cache successful and intentional no-op outcomes. Publish original
  receipts first, then optional derivatives; separate retry and GC eligibility.
- CPU batch scheduling should use independent bounded work queues, deduplication
  per source hash plus policy, and a distributed lease only if multiple independent
  orchestrators actually require it. Avoid introducing another database prematurely.

## Local measurement

`python -m tools.benchmark_studio_media --output <empty-dir> --images <paths> --videos <paths>`

Requires Pillow with WebP/AVIF support and FFmpeg/FFprobe on PATH for video.
The benchmark stores only derivatives and a JSON report, verifies source hashes
before/after each trial, and never uploads media. It is not a production encoder.

For video quality comparisons with FFmpeg built with libvmaf:

`python -m tools.benchmark_video_quality --output <empty-dir> <video-paths>`

This measures VMAF, SSIM and PSNR against decoded originals at source resolution;
source files are hash-checked before/after the experiment.

## Gallery derivatives (quality-first)

`python -m tools.build_studio_derivatives` creates a bounded 640px WebP cache
from checksum-matched local sources. Add `--remote` to retrieve missing
sources from HF, preferring an independently verified full-resolution preview
for the thumbnail when available. Add `--publish` to publish and verify only
the content-addressed WebP derivatives, not the original media. A local index
at `outputs/.runtime/gallery-derivatives.json` checkpoints each batch.

Studio uses `gallery_thumb` for browsing, `preview` for the image viewer, and
`url` for original image/video downloads and original MP4 playback. The monitor
polling path does not encode or download media. The index is a separate cache;
existing gallery recovery manifests do not include these new thumbnails yet.

After generating the cache, rebuild the frontend if its source changed and
run `python -m orchestration.studio` to regenerate `outputs/index.html`.
