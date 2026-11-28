# Durable media delivery protocol 3 - release candidate

V3 is an opt-in protocol for new Hugging Face or both-destination task plans.
It does not change frozen protocol-1/2 plans, existing media, or existing model
apps. Do not change the protocol of an existing scene to append new shots.

## Deploy independently

1. Create a dedicated persistent Volume V2 if it does not already exist:
   `uv run modal volume create delivery-store --version 2`.
   The model/result Volume remains `model-store`; its layout is unchanged.
2. Set the same optional process environment on the deployer and task registrar:
   `DELIVERY_V3_VOLUME_NAME=delivery-store` and
   `DELIVERY_V3_APP_NAME=modal-vllm-sglang-delivery-v3`.
   `DELIVERY_V3_REGION=auto` uses one automatically placed CPU pool.
   A fixed region can be configured at deployment; do not dynamically create
   independent regional pools. Scheduled scans are enabled by default.
3. Deploy only the CPU App:
   `uv run modal deploy pipeline/deploy_delivery_v3.py`.
   The existing Hugging Face secret must authorize the public destination bucket.
   Task registration checks protocol and both actual Volume identities.
4. Opt in to V3 on a **new scene**:
   ```json
   {
     "delivery_protocol": 3,
     "delivery": {
       "provider": "huggingface",
       "compression": "none",
       "verify": "sha256"
     }
   }
   ```
   If compression is omitted, V3 preserves the original bytes. Explicit
   adaptive lossless WebP remains supported. Verification is always SHA-256.
5. For direct completion events, deploy the new model runtime and workflow
   controller separately through the existing deployment workflow. Older model
   deployments ignore the additional event context; the scheduled scanner
   remains sufficient for already-submitted generation calls.

## Durable boundaries

- The active intent contains the full immutable generation locator and frozen
  delivery policy. Write it before submitting generation. Copy the same intent
  into the history ledger; a lost copy can be repaired from active.
- The model commits media and result metadata first. A best-effort notification
  submits only the delivery ID after releasing the model's generation lock.
  It has a two-second local await deadline and never waits for HF upload.
- The CPU App reads the media Volume via a read-only mount and writes the
  separate metadata Volume only via the SDK. Metadata reads use a 10s deadline;
  writes, listing, deletion and source reload use 15s. A local timeout does not
  prove the remote mutation failed.
- The scan enumerates active markers only, rotates its bounded work and spawns
  independent batches without waiting for their completion. The single Modal
  archive function pool runs up to four containers; queued calls may exceed
  four. Recovery batches group up to eight originals from one bucket within
  64 MiB. Larger files run individually. Completion hints use five-second Queue partitions and a conditional wake-up key; the worker waits until the window closes plus one second, drains at most 32 hints, deduplicates, and groups ready assets. Late or lost hints remain recoverable from Volume. No always-on worker is required.
- Claims use immutable attempt records and a lease longer than the 600s
  worker timeout. Positive platform terminal hints may accelerate takeover;
  absent or ambiguous call graphs never imply termination. A lease is only
  duplicate-work suppression, not a correctness guarantee. Publication is
  content-addressed and receipts cannot overwrite an existing record.
- The shared archive engine reports verified assets immediately from its
  verification threads. A separate bounded receipt buffer flushes up to four
  ready receipts or after 50ms; it never waits for an unfinished verification.
  On uncertain/conflicting SDK submission, validate existing receipts and
  retry only missing records. Never enable force-overwrite.
- After a verified receipt exists, durably register its derivative task before
  removing the active marker. A crash between these steps is repairable.
  The same media pool executes original and derivative work. It reuses verified
  private source snapshots or verified encoded bytes after the receipt buffer
  closes. Recovery falls back to verified Volume copies or streamed HF reads.
  The legacy derivative RPC forwards to the shared pool and does no media work.
  Originals and derivatives use separate append-only retry histories. Derivative
  failure cannot invalidate an original receipt. Eight failed derivative attempts
  or a permanent classified failure produce a failed derivative receipt.
  Scans run every minute for originals and every second minute for derivatives.
  They rotate through 16 hash shards and the legacy flat directory, with shared
  limits of 128/64 examinations and 32/16 selections respectively. Each shard
  keeps its own cursor. Listing is still complete within a shard; no hard
  end-to-end recovery SLA is claimed.
  Conditional Dict inserts reserve both adjacent 660-second clock slots.
  Terminal predecessor evidence permits an append-only next ownership generation.
  Legacy boolean reservations are retained until their window expires.
  The existing scanner attempts coordination GC every ten minutes: only known
  time-bucket keys at least 24 hours past their window are eligible, with 16
  concurrent deletes, 256 attempts, 4096 examined keys and an 8s work budget.
  GC does not touch durable records. Enumeration fairness and sustained throughput
  are not guaranteed. Time-partitioned Dict deletion is not part of this release.
- The metadata Volume contains `/inference/delivery-v3/active` and
  `/inference/delivery-v3/derivatives`. Its immutable history deliberately
  retains the V2 schema/path (`/inference/delivery-v2/...`) to reuse validation
  code; this is physically separate from protocol 2's original Volume.

## Frozen candidate configuration

Media pool: 4 containers, 1 CPU, 2048 MiB, 600s timeout. Default scale-down
is 60s; optional deployment setting DELIVERY_V3_MEDIA_SCALEDOWN accepts
10, 20, 30 or 60. Do not change the default based on a synthetic reuse test
alone. This is a protocol-3 implementation revision, not a new package
version or an automatic production cutover.

Upgrade order: quiesce old delivery workers and scanner before replacing the CPU
App. New workers read both legacy and sharded markers. Older workers do not
necessarily discover sharded markers, so do not roll back to an older scanner
until pending sharded markers have been drained or explicitly reconciled.
Generation deployments using singleton notifications remain compatible.

## Limits / operation

Run `python -m benchmarks.delivery_v3_audit` to exercise concurrent ownership
against a real ephemeral Modal Dict and replay a mixed valid/damaged PNG batch
in a remote CPU worker. The harness seeds original receipts in an isolated
Volume, verifies both derivative outcomes and checks duplicate delivery. It
requires the configured Modal workspace and the worker's existing HF secret.
The original-upload path is outside this audit.

A fresh delivery still performs full remote SHA-256 verification, including
recovering a remote object whose receipt was lost. Existing validated delivery
receipts are reused; remote Xet metadata-only verification is **not** enabled
because the earlier test did not establish a sufficiently strong atomic
metadata/readback contract. Do not treat Xet Hash as SHA-256.

A stuck native SDK call may still hit the Modal function hard timeout. Scanning
is a recovery mechanism, not a guarantee of an exact delivery deadline.
Historical receipts and attempt evidence remain immutable; do not delete
blocked records to force retries. There is not yet an operator requeue CLI.
Review the failed delivery and use the existing incident procedure.

The V3 opt-in does not migrate existing work or automatically switch production
default behavior. The new CPU App and new model runtime require separate
deployment. Anonymous test Volume IDs are accepted only for an explicitly named
unscheduled lab App. Never point a lab at production resources.
