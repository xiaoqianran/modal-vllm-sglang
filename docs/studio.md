# Modal Studio

The canonical browser entry is `outputs/index.html`. The Vue application
contains the gallery and task monitor; both share typography, theme, controls,
and the media viewer. Existing task monitor HTML files are kept as compatible
historical links. Nothing here deploys an HTTP server.

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
