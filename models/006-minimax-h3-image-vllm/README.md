# MiniMax H3 Image - vLLM-Omni

Independent candidate service. Uses minimax-omni.t2i and minimax-omni.i2i; the original minimax service remains SGLang. Shares checkpoint and validator. GPU acceptance required before deployment. Text encoder layer offload, 24 GiB host RAM, max_inputs=2, target_inputs=1, max_containers=2 (one GPU per container; scale to zero; one active engine request plus input preparation or durable publication), GPU snapshot enabled with 2048x1152 six-step warmup; capture and restore pending validation.


The formal SDK image path verifies input references before acquiring the engine lock. It keeps the submitting checkpoint before the engine POST and uses the existing bounded result writer after releasing the engine lock. The per-output claim is retained until publication completes; the existing SDK submission ledger remains the cross-container admission authority. The vLLM-Omni Base64 response protocol is unchanged pending native-file transport verification.


### Elastic scaling diagnosis

The configured cap is two containers with one GPU each, two inputs per
container, a target of one input and one extra buffer container under active
load. The minimum remains zero, so no GPU is kept permanently warm.
Buffering is an autoscaler request, not a guarantee of GPU availability;
it may incur idle GPU time while the service is active.

Run `python run.py check-scaling minimax-omni` to compare the deployed
function's actual autoscaler and input-concurrency settings with model.json,
and inspect backlog, runners, active inputs and headroom without starting
a GPU or changing autoscaler settings. A single sample cannot distinguish
resource scarcity, workspace quotas, snapshot startup and autoscaler delay.

For the next real production acceptance: record a cold and warm 24-image
batch, save repeated check-scaling samples and correlate completed image
IDs and container IDs from the app logs. Require two distinct GPU containers
to complete work, verify the backlog drains, all image receipts and SHA-256
checks succeed, and the service scales to zero when idle. Do not reuse an
already accepted task or mix deployment hashes. Compare total elapsed time,
GPU allocation time and cost against the previous deployment.
