# Modal Studio

Start the local Vue Studio with `python run.py studio`. It listens on
`127.0.0.1:18765` by default and opens the browser. Use `--port` to change
the port or `--no-browser` to skip opening a tab. Opening the Studio reads
local records. Submitting a task or choosing **同步任务** starts local
scheduling. A scene can have one local scheduler at a time across Studio
and the CLI worker. Restarting Studio restores the last viewed task from
local status without starting its scheduler.

Wait for the Studio to indicate that it is safe to close. For a batch,
every unfinished item needs evidence that its remaining cloud work can
continue independently. HF delivery also needs a durable V3 delivery intent
or a verified archive. Tasks requesting a local copy wait for local
collection. If acceptance is uncertain, use **同步任务** to check
existing records. If an existing remote DAG controller has terminated,
choose **恢复云端执行** to request guarded recovery. A blocked HF archive
needs attention even when local scheduling can stop. See
[Runtime Reliability Design](runtime-reliability-design.md).

The standalone gallery entry is `outputs/index.html`. The Vue application
contains the gallery and task monitor, with shared controls and a media
viewer. The worker publishes task data for this frontend.
Historical thumbnail references can be stored in
`outputs/.runtime/studio-history-media.json` under an `assets` array.
## Build and preview

```powershell
cd frontend
npm ci
npm run build
cd ..
uv run python -m orchestration.studio
```

Open `outputs/index.html` directly. The bundled template under
`orchestration/studio_dist/index.html` is generated with Vite's single-file
plugin. End users do not need Node.js. Source changes require rebuilding the template. The gallery never embeds
image bytes as Base64. It references verified HF previews, falling back to the
original URL when a preview is unavailable. Local images that have not yet
been migrated are referenced by their relative file path.

The existing monitor writer updates the lightweight local
`outputs/.runtime/studio-catalog.js` on its normal refresh interval. New assets
and tasks appear in Studio; the selected task reads its existing detailed feed.
Missing previews fall back to the original media URL. No image payload is copied into the HTML. The live polling loop never downloads or encodes media.
Historical migration receipts are preferred only when their original hash and
size match the task's local artifact record. The original media and task records
are not deleted by the Studio builder.
