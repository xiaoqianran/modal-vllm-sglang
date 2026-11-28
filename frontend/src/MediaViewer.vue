<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { X, ChevronLeft, ChevronRight, ExternalLink, Copy, Maximize2, Play } from 'lucide-vue-next'
const props = defineProps({ asset:Object, position:Number, total:Number })
const emit=defineEmits(['close','step','create'])
const loaded=ref(false),failed=ref(false),copied=ref(false),dialog=ref(null)
watch(()=>[props.asset?.project,props.asset?.id,props.asset?.url],()=>{loaded.value=false;failed.value=false;copied.value=false})
const src=computed(()=>loaded.value?props.asset?.url:((props.asset?.kind==='image'?props.asset?.preview||props.asset?.thumb||props.asset?.url:props.asset?.thumb||'')))
function imageError(){if(!loaded.value&&props.asset?.url&&src.value!==props.asset.url){loaded.value=true}else{failed.value=true}}
function key(e){if(e.defaultPrevented||e.isComposing||e.altKey||e.ctrlKey||e.metaKey||e.target?.closest?.('input,textarea,select,[contenteditable]'))return;const delta=e.key==='ArrowLeft'?-1:e.key==='ArrowRight'?1:0;if(!delta)return;e.preventDefault();e.stopPropagation();if(props.position+delta>=0&&props.position+delta<props.total)emit('step',delta)}
async function copy(){try{await navigator.clipboard.writeText(props.asset.url);copied.value=true}catch{window.prompt('复制文件地址',props.asset.url)}}
onMounted(()=>{dialog.value.showModal();document.addEventListener('keydown',key,true)})
onUnmounted(()=>document.removeEventListener('keydown',key,true))
</script>
<template>
<dialog ref="dialog" class="viewer" @cancel.prevent="emit('close')" @click.self="emit('close')">
  <div class="viewer-head"><div><small>{{asset.project}}</small><h2>{{asset.title||asset.name}}</h2></div><button class="icon-btn" aria-label="关闭预览" @click="emit('close')"><X :size="22"/></button></div>
  <div class="viewer-stage">
    <img v-if="asset.kind==='image'&&src&&!failed" :src="src" :alt="asset.title||asset.name" @error="imageError">
    <video v-else-if="asset.kind==='video'&&loaded&&!failed" :key="asset.id" :src="asset.url" :poster="asset.thumb||undefined" controls autoplay playsinline preload="none" @error="failed=true"/>
    <img v-else-if="asset.thumb&&!failed" :src="asset.thumb" :alt="asset.name">
    <span v-else>{{failed?'文件暂时无法加载':'点击下方按钮加载媒体'}}</span>
    <button v-if="!loaded&&!failed&&asset.url&&(asset.kind==='video'||src!==asset.url)" class="load-original" @click="loaded=true"><component :is="asset.kind==='video'?Play:Maximize2" :size="17"/> {{asset.kind==='video'?'播放视频':'查看高清原图'}}</button>
    <button v-if="failed" class="load-original" @click="failed=false;loaded=false">返回预览</button>
    <button class="viewer-prev" aria-label="上一项媒体" title="上一项媒体 (←)" aria-keyshortcuts="ArrowLeft" :disabled="position===0" @click="emit('step',-1)"><ChevronLeft/></button>
    <button class="viewer-next" aria-label="下一项媒体" title="下一项媒体 (→)" aria-keyshortcuts="ArrowRight" :disabled="position>=total-1" @click="emit('step',1)"><ChevronRight/></button>
  </div>
  <div class="viewer-foot"><div><strong>{{position+1}} / {{total}}</strong><span> · {{asset.kind==='video'?'视频':'图片'}}</span><span v-if="total>1" class="viewer-key-hint"> · ← → 切换</span><span v-if="asset.bytes"> · {{(asset.bytes/1048576).toFixed(1)}} MiB</span><span v-if="asset.dimensions"> · {{asset.dimensions.join(' × ')}}</span></div><div class="actions"><button v-if="asset.kind==='image'" @click="emit('create',asset)">使用此图创作</button><button v-if="asset.url" @click="copy"><Copy :size="15"/> {{copied?'已复制':'复制链接'}}</button><a v-if="asset.url" :href="asset.url" target="_blank" rel="noopener noreferrer">原文件 <ExternalLink :size="14"/></a></div></div>
  <details v-if="asset.prompt" class="viewer-prompt"><summary>生成提示词</summary><p>{{asset.prompt}}</p></details>
</dialog>
</template>
