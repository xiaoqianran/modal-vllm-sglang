# MOSS storyboard batch: continuous generation and reliable resume

Deploy MOSS first; use **one deployed Class method call** for the entire batch. MOSS HTTP and SDK calls share one GPU Class, one vLLM process per container, and a Class-level one-container limit. The older separate GPU functions must be allowed to finish before redeploying this unified version.

## Standard entrypoint (new dramas and existing dramas)

```powershell
uv run python 001-moss-image/run.py --task tasks/002-again-love-001-hotel-corridor.json --task tasks/002-again-love-002-social-media.json --task tasks/002-again-love-003-apartment-distance.json --task tasks/002-again-love-004-lip-reading.json --task tasks/002-again-love-005-hidden-video.json
```

There are no implicit default GPU tasks. Always pass at least one `--task`; the project is inferred from the task JSON. `--project` remains an optional override. Historic source `generate_again_love_v3.py` was folded into `001-moss-image/run.py`.

- Load and validate all JSON tasks **before GPU submission**; use unique compound remote shot IDs across scenes.
- Compute a stable batch remote prefix from the full ordered prompt list.
- Skip existing local images, verifying any available image manifest and SHA256.
- Submit **one Modal Class generator method call** for all missing shots. vLLM is initialized once per shared container and can be reused by subsequent SDK or HTTP inputs while the container remains warm.
- After each inference the Modal function atomically writes an image + signed intent sidecar to the Volume, then **commits** before emitting an event.
- PC2 receives each event and immediately downloads/checks the corresponding image. Image and progress metadata are stored atomically.
- After interruption, reissue **the exact same command**. Existing locally verified files are preserved; missing images previously committed on the Volume are emitted as `skipped` and fetched without inference; genuinely missing shots are generated.
- Outputs remain `outputs/<project>/<scene>/<shot>.png`, with metadata in `outputs/<project>/.runner/moss-v3-progress.json` and `outputs/<project>/<scene>/.runner/moss-v3/<shot>.json`.
- A local OS lock prevents concurrent batch clients from writing to the same drama.
- An explicit `--dry-run` avoids all GPU use. If no missing images remain, the normal command also avoids GPU use.

**Recovery of the prior non-streaming implementation**: when a past job generated images remotely but ended before local download, run the same command with `--recover-prefix <known-output-prefix>`. This retrieves verified remote artifacts without GPU before submitting any remaining tasks.

**Boundaries**: If a remote GPU container dies, restarting requires a new GPU allocation and vLLM startup (the previous process cannot survive container loss); completed committed images are not regenerated. A remote-side image error is reported and does not result in successful completion. Existing files with mismatched manifests are rejected rather than overwritten. There is no automatic retry of ambiguous concurrent remote calls; verify the first job stopped before relaunching.

For a changed model or sampling configuration, do not reuse earlier remote artifacts with incompatible manifests; start a fresh versioned task/output rather than overwrite previous images.
