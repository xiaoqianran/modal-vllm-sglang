<script setup>
import { ref, watch, onUnmounted } from 'vue'
import { Copy, Check, AlertCircle } from 'lucide-vue-next'
import { copyPrompt } from './prompt-clipboard.js'
const props = defineProps({ text: { type: String, default: '' } })
const button = ref(null), state = ref('idle')
let timer, generation = 0
function reset() { clearTimeout(timer); generation++; state.value = 'idle' }
watch(() => props.text, reset)
onUnmounted(reset)
async function copy() {
  reset()
  const id = generation
  const ok = await copyPrompt(props.text, button.value)
  if (id !== generation) return
  state.value = ok ? 'copied' : 'failed'
  timer = setTimeout(reset, 2200)
}
</script>
<template>
  <button ref="button" type="button" class="prompt-copy" :data-state="state" :disabled="!text.trim()" @click.stop="copy" :title="state === 'failed' ? '请手动选择提示词并复制' : '复制完整提示词'">
    <component :is="state === 'copied' ? Check : state === 'failed' ? AlertCircle : Copy" :size="14" aria-hidden="true"/>
    <span aria-live="polite">{{ state === 'copied' ? '已复制' : state === 'failed' ? '复制失败' : '复制提示词' }}</span>
  </button>
</template>
<style scoped>
.prompt-copy{display:inline-flex;align-items:center;justify-content:center;gap:6px;flex:none;padding:5px 9px;min-height:30px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--ink);font:500 12px/1.3 system-ui,sans-serif;white-space:nowrap}
.prompt-copy:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
.prompt-copy:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.prompt-copy[data-state=copied]{color:var(--accent);border-color:var(--accent)}
.prompt-copy:disabled{opacity:.45;cursor:not-allowed}
</style>
