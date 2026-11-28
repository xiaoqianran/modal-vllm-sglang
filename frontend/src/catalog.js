// Shared catalog reconciliation for static feeds and task detail previews.
const cloud = url => typeof url === 'string' && url.startsWith('https://')
const labels = { image: '图片', first: '首帧', last: '尾帧', video: '视频' }

export function mergeSnapshot(previous, incoming, summary = false) {
  // A delayed summary must not roll back a more recent detailed record.
  if (Number.isFinite(previous?.updated_at) && Number.isFinite(incoming.updated_at)
      && incoming.updated_at < previous.updated_at) return previous
  if (!summary) return incoming
  const { jobs, ...fields } = incoming
  return { ...previous, ...fields }
}

export function mergeTaskAssets(assets, snapshot) {
  const byId = new Map(assets.map(asset => [asset.id, asset]))
  const byLocal = new Map()
  const index = asset => {
    if (asset.local_url) byLocal.set(asset.local_url, asset)
    if (asset.url && !cloud(asset.url)) byLocal.set(asset.url, asset)
  }
  assets.forEach(index)
  for (const job of snapshot.jobs || []) {
    const media = job.asset
    if (!media?.url || !['image', 'video'].includes(media.kind)) continue
    const id = snapshot.name + '/' + job.key
    const local = media.local_url || (!cloud(media.url) ? media.url : '')
    const alias = local && byLocal.get(local)
    const existing = byId.get(id) || alias
    // A verified catalog alias can retain its canonical HF address when an
    // older monitor feed still refers to the corresponding local file.
    const url = !cloud(media.url) && alias && cloud(alias.url) ? alias.url : media.url
    if (existing) {
      const changed = existing.url !== url
      if (changed) {
        for (const key of [existing.local_url, existing.url]) {
          if (byLocal.get(key) === existing) byLocal.delete(key)
        }
        delete existing.gallery_thumb
        delete existing.preview
        delete existing.artifact
        delete existing.digest
        existing.thumb = ''
        existing.dimensions = undefined
        existing.bytes = undefined
        if (existing.local_url !== local) delete existing.local_url
      }
      existing.url = url
      existing.storage = cloud(url) ? 'cloud' : 'local'
      if (local && cloud(url)) existing.local_url = local
      if (job.prompt) existing.prompt = job.prompt
      if (media.bytes != null && url === media.url) existing.bytes = media.bytes
      if (media.dimensions) existing.dimensions = media.dimensions
      // Do not replace a verified gallery derivative or full-size preview
      // with a local path from an older feed.
      if (media.thumb && (url === media.url || cloud(media.thumb))) {
        if (!existing.gallery_thumb) {
          if (!existing.thumb || cloud(media.thumb) || !cloud(existing.thumb)) existing.thumb = media.thumb
        } else if (existing.kind === 'image' && media.thumb !== existing.gallery_thumb) {
          existing.preview = media.thumb
        }
      }
      if (!existing.thumb && media.kind === 'image') existing.thumb = url
      if (media.artifact && url === media.url) existing.artifact = media.artifact
      index(existing)
      byId.set(id, existing)
      continue
    }
    const filename = media.filename || job.key
    const asset = {
      id, project: snapshot.name,
      name: /^[a-f0-9]{64}\./.test(filename) ? job.item + '_' + job.step + '.' + filename.split('.').pop() : filename,
      title: job.item + ' · ' + (labels[job.step] || job.step),
      url, kind: media.kind, bytes: media.bytes, dimensions: media.dimensions,
      prompt: job.prompt, thumb: media.thumb || (media.kind === 'image' ? url : ''),
      storage: cloud(url) ? 'cloud' : 'local',
      ...(local && cloud(url) ? { local_url: local } : {}),
      ...(media.artifact ? { artifact: media.artifact } : {}),
    }
    assets.push(asset)
    // Retrieve the reactive object from the array, not its raw input.
    const added = assets[assets.length - 1]
    byId.set(id, added)
    index(added)
  }
}

export function monitorAsset(job, project, mediaIndex = {}, thumbs = {}) {
  const media = job?.asset
  if (!media?.url) return null
  const indexed = mediaIndex[project + '/' + job.key] || mediaIndex[media.url]
  const match = indexed?.project === project && indexed.url === media.url ? indexed : undefined
  return {
    ...match,
    id: job.key, project, title: match?.title || labels[job.step] || job.step,
    name: match?.name || media.filename, kind: media.kind, url: media.url,
    thumb: match?.thumb || thumbs[media.url] || media.thumb || '',
    preview: media.kind === 'image' ? match?.preview || (match?.gallery_thumb ? media.thumb : match?.thumb) || media.thumb || '' : '',
    bytes: match?.bytes ?? media.bytes, dimensions: media.dimensions || match?.dimensions,
    prompt: job.prompt || match?.prompt,
    ...(media.artifact ? { artifact: media.artifact } : {}),
  }
}

export function galleryPreview(asset, allowRemoteOriginal = false) {
  if (!asset) return ''
  if (asset.thumb) return asset.thumb
  return asset.kind === 'image' && (!cloud(asset.url) || allowRemoteOriginal) ? asset.url || '' : ''
}

export function canFallbackToOriginal(asset, allowRemoteOriginal = false) {
  return asset?.kind === 'image' && !!asset.url && (!cloud(asset.url) || allowRemoteOriginal)
}
