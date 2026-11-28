import { onMounted, onUnmounted, ref } from 'vue'
import { getCatalog } from './api.js'
import { mergeCatalogTasks, mergeLiveAssets, mergeSnapshot, mergeTaskAssets } from './catalog.js'

// Own the lifecycle of both catalog transports, not task execution.
export function useCatalogSync(initial, assets, tasks, selected) {
  const connection = ref('idle')
  let timer, script, timeout, pending = false, disposed = false
  function updateTask(snapshot, summary = false) {
    const task = tasks.value.find(t => t.snapshot.name === snapshot.name)
    if (!task) return
    const next = mergeSnapshot(task.snapshot, snapshot, summary)
    if (next === task.snapshot) return
    task.snapshot = next
    mergeTaskAssets(assets.value, snapshot)
  }
  function selectFirst() {
    if (!selected.value) selected.value = tasks.value[0]?.id || ''
  }
  async function syncLive() {
    if (pending || disposed) return
    pending = true
    try {
      const data = await getCatalog()
      if (disposed) return
      if (Array.isArray(data.assets)) assets.value = mergeLiveAssets(assets.value, data.assets)
      mergeCatalogTasks(tasks.value, data.tasks)
      selectFirst()
      connection.value = 'connected'
    } catch {
      if (!disposed) connection.value = 'unavailable'
    } finally { pending = false }
  }
  function clearScript() {
    clearTimeout(timeout)
    script?.remove()
    script = null
  }
  function receive(list) {
    if (disposed || !Array.isArray(list)) return
    for (const t of list) {
      if (!tasks.value.some(x => x.id === t.id)) tasks.value.push({...t, snapshot:{...t.snapshot, jobs:[]}})
      updateTask(t.snapshot, true)
    }
    selectFirst()
    connection.value = 'connected'
    clearScript()
  }
  function loadStatic() {
    if (disposed || !initial.catalog_url || script) return
    window.updateStudioCatalog = receive
    script = document.createElement('script')
    script.src = initial.catalog_url + '?t=' + Date.now()
    script.onerror = () => { connection.value = 'unavailable'; clearScript() }
    script.onload = clearScript
    document.body.appendChild(script)
    timeout = setTimeout(() => { connection.value = 'unavailable'; clearScript() }, 8000)
  }
  onMounted(() => {
    if (initial.live_studio) { syncLive(); timer = setInterval(syncLive, 15000) }
    else if (initial.catalog_url) { loadStatic(); timer = setInterval(loadStatic, 5000) }
  })
  onUnmounted(() => {
    disposed = true
    clearInterval(timer)
    clearScript()
    if (window.updateStudioCatalog === receive) delete window.updateStudioCatalog
  })
  return { updateTask, connection }
}
