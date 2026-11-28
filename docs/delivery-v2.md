# Durable media delivery (protocol 2)

Protocol 2 is an opt-in deployment path. Existing task definitions and frozen plans
without `delivery_protocol: 2` continue to use the legacy archiver unchanged.

## Enable for new tasks

Add `"delivery_protocol": 2` at the top level of a new task using Hugging Face
delivery (or combined local/HF delivery). The protocol is frozen in its plan.
Deploy the independent CPU app before admitting new protocol-2 tasks:

```powershell
uv run modal deploy pipeline/deploy_delivery_v2.py
```

Use the existing workflow deployment/admission process for new cloud-controlled
workflows so their controller includes the updated submission code. Do not
redeploy or rewrite active runs just to migrate them. The readiness check tests
the protocol, deployed build integrity and Volume identity; it does not require
the PC2 source tree to have the same hash as the remote deployment.

## Recovery and limits

- The intent is written before submitting generation. A scheduled CPU scanner
  discovers ready intents once per minute even when PC2 is offline.
- A per-attempt immutable claim protects publication. Terminal call evidence or
  lease expiry permits recovery; a completed receipt prevents redispatch.
- Original publication verifies SHA-256, uses content-addressed storage and
  reuses existing verified assets. A receipt is durable evidence of that
  verification, not a fresh audit of the HF object on every poll.
- Batches contain 1-8 files, grouped by bucket and bounded by a 64 MiB aggregate
  target (a single larger file is allowed). Up to four batches are dispatched
  concurrently. There is no minimum wait for four ready files.
- Preferred CPU region is us-east. Set DELIVERY_V2_REGION=auto for platform
  placement. Confirmed initialization failure or dispatch failure records a
  fallback; subsequent scans use default placement rather than immediately
  racing a second publisher.
- Retryable errors use bounded exponential backoff and Retry-After. Auth,
  integrity and exhausted retries are blocked for operator inspection; an
  operator reset CLI is not implemented. Do not delete immutable evidence to
  bypass a block.
- Original receipts do not depend on preview/poster success. A separate CPU
  scanner runs every two minutes. Video posters require the video_poster module
  and ffmpeg; images that do not benefit from a preview retain the original.
- No source media or historical assets are deleted by delivery. Scheduled
  scanners scale down but their periodic execution still incurs compute costs.

## Isolation

For experiments, use a dedicated pre-created V2 Volume, separate public HF
bucket and DELIVERY_V2_APP_NAME. DELIVERY_V2_SCHEDULE=0 disables Cron for manual
tests. Never use a production bucket or Volume for destructive fault injection.
