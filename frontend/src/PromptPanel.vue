<script setup>
import PromptCopyButton from './PromptCopyButton.vue'
defineProps({ text: { type: String, default: '' } })
</script>
<template>
  <section v-if="text.trim()" class="prompt-panel" aria-label="生成提示词">
    <div class="prompt-panel-head"><strong>生成提示词</strong><PromptCopyButton :text="text"/></div>
    <div class="prompt-panel-text" tabindex="0">{{ text }}</div>
  </section>
</template>
<style scoped>
.prompt-panel{min-width:0;margin:10px 0;padding:12px 14px;border:1px solid var(--line);border-inline-start:3px solid var(--accent);border-radius:6px;background:var(--soft);background:color-mix(in srgb,var(--accent) 9%,var(--surface));color:var(--ink)}
.prompt-panel-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}
.prompt-panel-head strong{font-size:12px;font-weight:650;color:var(--accent)}
.prompt-panel-text{font:400 13px/1.7 system-ui,sans-serif;white-space:pre-wrap;overflow-wrap:anywhere;max-height:180px;overflow:auto;user-select:text}
.prompt-panel-text:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
</style>
