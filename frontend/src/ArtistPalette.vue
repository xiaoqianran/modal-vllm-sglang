<script setup>
import { computed } from 'vue'
import { Palette, X, Check } from 'lucide-vue-next'
const props=defineProps({themes:Array,modelValue:String,open:Boolean})
const groups=computed(()=>[{name:"Catppuccin",items:props.themes.filter(t=>t.collection==="catppuccin")},{name:"Artist collection",items:props.themes.filter(t=>t.collection!=="catppuccin")}].filter(g=>g.items.length))
defineEmits(['update:modelValue','update:open'])
</script>
<template>
<div class="artist-palette" @keydown.esc.stop="$emit('update:open',false)">
<button class="palette-toggle" aria-label="艺术家调色盘" :aria-expanded="open" aria-controls="artist-palette-options" @click="$emit('update:open',!open)"><Palette :size="18"/><span class="palette-toggle-name">Palette</span><span class="palette-current-dot"/></button>
<section v-if="open" id="artist-palette-options" class="palette-panel" aria-label="Artist Palette">
<header class="palette-panel-head"><div><h2>Artist Palette</h2><p>为你的电影世界选择色彩</p></div><button aria-label="关闭调色盘" @click="$emit('update:open',false)"><X :size="19"/></button></header>
<div v-for="group in groups" :key="group.name" class="palette-group"><h3>{{group.name}}</h3><div class="palette-options">
<button v-for="t in group.items" :key="t.id" :data-palette-option="t.id" :aria-pressed="modelValue===t.id" class="palette-option" @click="$emit('update:modelValue',t.id)">
<div class="palette-preview" :style="{background:t.paper,color:t.ink}"><span>Aa</span><div class="palette-swatches"><i v-for="c in [t.ink,t.accent,t.pop]" :key="c" :style="{background:c}"/></div><Check v-if="modelValue===t.id" :size="15" class="palette-check"/></div>
<strong>{{t.name}}</strong><small>{{t.cn}}</small>
</button>
</div>
</div><button class="palette-reset" @click="$emit('update:modelValue','neo')">恢复默认 Neo Surrealism</button>
</section>
</div>
</template>
