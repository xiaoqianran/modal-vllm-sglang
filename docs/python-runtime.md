# Local and remote Python version independence

The local CLI and Modal containers do not need the same Python minor version.
The supported local versions are constrained by the Modal SDK and the local
project's dependency requirements; cloud versions are selected by the engine
and wheel ABI, never by sys.version_info on the client.

| Boundary | Default cloud Python | Code / data transport |
| --- | --- | --- |
| HF Coordinator and Writer (modal-benchmark) | 3.15 | importable module-level functions; JSON config, writer ID |
| Inference cache publisher, maintenance, legacy preparer, provision info | 3.12 | importable module-level functions; ordinary arguments or JSON environment |
| Media GPU runtime (MOSS/Pika/MiniMax) | 3.12 | importable module-level classes; mounted model.json |
| ComfyUI GPU / cache maintenance | 3.11 | module-level class and functions; explicit runtime environment |

## Registration rules

- Use serialized=False for remote function/class definitions. Avoid closures,
  dynamically defined classes and serialized local function objects.
- Pass JSON-compatible configuration and identifiers. Reconstruct Modal Volume
  handles inside the container using the selected volume name/version.
- Keep the public deployed names Moss, Pika, H3 and the default Model.
  Snapshot and non-snapshot variants have the same class tag but live in
  separate importable modules. New custom tags require an explicit module-level
  class; ordinary declarative models can keep Model.
- Register web server metadata with the deployment spec locally, and recreate
  the source method with the same port in the remote interpreter. Runtime
  environment variables are passed via app.cls(env=...), not by adding image
  build steps after copy=False mounts.
- Legacy download(refresh, migrate_only, repair), check_cache() and
  provision_info() keep their existing RPC signatures. Legacy cache configuration
  is JSON in the remote environment rather than a captured closure.
- The GPU runtime reads /opt/model.json, reuses prevalidated caches and separates
  snapshot engine startup from post-restore gateway/monitor initialization.
  The CPU cache workers use the locally validated cache spec without importing
  GPU-specific request validators.
- A caller needing a different post-processing Python version declares its
  own explicit Image instead of using a local-version-matching image.

## Evidence

- Modal SDK 1.6.4, Windows local Python 3.12.13 to cloud Python 3.11.18:
  independent module-level function and class invocation passed.
- modal-benchmark source: local 3.12.13 to cloud 3.15.2; real
  sshleifer/tiny-gpt2/config.json (662 bytes) downloaded, persisted and
  verified in 11.507 s; temporary volume deleted.
  https://modal.com/apps/zhangchenzer/main/ap-se2mjz9JrF7gEjl1cBgIZn
- Inference cache maintenance: local Python 3.14.7 to remote 3.12,
  MOSS cache fingerprint verified without downloads.
  https://modal.com/apps/zhangchenzer/main/ap-9qBItdDjmYlB5ZVG0B5HrG
- MOSS GPU class: local 3.14.7 to cloud 3.12, original Moss tag,
  provision info, deployment_info, actual generation, PNG 1024x1024
  decoding and SHA-256 verification passed. Isolated app, snapshots off.
  https://modal.com/apps/zhangchenzer/main/ap-9URUq5BlRYbm8q4IMYYZct
- ComfyUI: local 3.14.7 to remote 3.11, actual quick_check function
  returned the expected missing-file result without downloading weights.
  https://modal.com/apps/zhangchenzer/main/ap-EHExVP5LQkdmJnDY0AXX65

The test matrix demonstrates version independence, not unrestricted
compatibility with every future Python or SDK version. CUDA wheel ABI,
source syntax, RPC data compatibility and actual dependencies remain
independent acceptance requirements.

- Full download and cache publication: local 3.14.7, fixed Python 3.15 download workers and Python 3.12 cache publisher; tiny GPT-2 config and weights (2,514,808 bytes) verified and cache-ready manifest published. Subsequent cache maintenance hit a concurrent local bytecode mount race; source mounts now explicitly exclude bytecode. Isolated test volume deleted. https://modal.com/apps/zhangchenzer/main/ap-O2LJUqq8Azbi3nyk1zZg7a
- Snapshot-enabled MOSS deployment registered and started successfully twice from local 3.14.7. Both containers logged GPU snapshot creation, but engine initialization was repeated (39.44 s and 58.45 s). Snapshot reuse is not verified; the temporary deployment was stopped. https://modal.com/apps/zhangchenzer/main/ap-XNfZevrX3qSDeKeaaAMxs2
- Production deployments have not been replaced. The archived LLM and ComfyUI GPU service have not been exercised end-to-end in this pass.
