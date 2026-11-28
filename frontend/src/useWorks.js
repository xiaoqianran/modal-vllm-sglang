import { computed, ref, onUnmounted } from 'vue'
import { getWorks, getWork } from './api.js'

// A transient view of HF directories. No localStorage, generated catalog or media cache.
export function useWorks(initial, assets) {
  const enabled = !!initial.live_studio && !!initial.remote_works
  const works = ref([])
  const error = ref('')
  const listingLoading = ref(false)
  const pendingDetails = ref(0)
  const loading = computed(() => listingLoading.value || pendingDetails.value > 0)
  const active = new Map()
  let listing, allLoading, disposed = false

  async function refresh() {
    if (!enabled || disposed) return []
    if (listing) return listing
    listingLoading.value = true
    listing = (async () => {
      try {
        const result = await getWorks()
        if (!Array.isArray(result.works)) throw new Error('Invalid HF directory response')
        if (disposed) return []
        const previous = new Map(works.value.map(w => [w.id, w]))
        const ids = new Set(result.works.map(w => w.id))
        works.value = result.works.map(w => ({...previous.get(w.id), ...w}))
        // Only a successful, complete directory response can remove a work.
        assets.value = assets.value.filter(a => ids.has(a.project))
        error.value = ''
        return works.value
      } catch (e) {
        if (!disposed) error.value = '无法读取 HF 作品目录：' + e.message
        return works.value
      } finally {
        listingLoading.value = false
        listing = undefined
      }
    })()
    return listing
  }

  function isRemote(id) {
    return enabled && works.value.some(w => w.id === id)
  }

  async function load(id) {
    if (!isRemote(id) || disposed) return null
    if (active.has(id)) return active.get(id)
    pendingDetails.value++
    const pending = (async () => {
      try {
        const detail = await getWork(id)
        if (detail.id !== id || !Array.isArray(detail.assets)) throw new Error('Invalid HF work response')
        if (disposed || !isRemote(id)) return null
        assets.value = [...assets.value.filter(a => a.project !== id), ...detail.assets]
        works.value = works.value.map(w => w.id === id ? {
          ...w, published: detail.published, expected: detail.expected,
          localTaskId: detail.local_task_id || null
        } : w)
        error.value = ''
        return detail
      } catch (e) {
        if (!disposed) error.value = '无法读取该作品：' + e.message
        return null
      } finally { active.delete(id); pendingDetails.value-- }
    })()
    active.set(id, pending)
    return pending
  }

  function loadAll() {
    if (!enabled || disposed) return Promise.resolve()
    if (allLoading) return allLoading
    allLoading = (async () => {
      const current = await refresh()
      // Explicit "all media" is the only operation that walks every work.
      const failures = []
      for (let i = 0; i < current.length && !disposed; i += 4) {
        const group = current.slice(i, i + 4)
        const results = await Promise.all(group.map(w => load(w.id)))
        failures.push(...group.filter((_, j) => !results[j]).map(w => w.name))
      }
      if (failures.length && !disposed) error.value = '部分作品读取失败：' + failures.join('、')
    })().finally(() => { allLoading = undefined })
    return allLoading
  }

  onUnmounted(() => { disposed = true })
  return { enabled, works, error, loading, refresh, load, loadAll, isRemote }
}
