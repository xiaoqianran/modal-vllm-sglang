<script setup>
import { computed, ref, watch, nextTick, onUnmounted } from 'vue'
import { gsap } from 'gsap'
import { ArrowUpRight, ArrowDown, ChevronLeft, ChevronRight, Play, Search, X } from 'lucide-vue-next'
const props=defineProps({assets:Array,groupLabel:Function})
const emit=defineEmits(['album','browse','preview','cover'])
const search=ref(''),page=ref(1),pageSize=8,chosen=ref('')
const names={'024-moss-before-summer-ends-12':['Before Summer Ends','夏日终章'],'025-moss-natural-dreamcore-12':['The Shape of Dreams','自然梦核'],'027-moss-impossible-nature-12':['Impossible Nature','不可能的自然'],'026-moss-surreal-cinema-12':['Beyond the Real','超现实电影'],'022-minimax-cinematic-atlas-12':['Cinematic Atlas','电影图鉴'],'015-isolated':['Before the Rain Stops','雨停之前']}
const albums=computed(()=>{const m=new Map();for(const a of props.assets){if(!m.has(a.project))m.set(a.project,[]);m.get(a.project).push(a)}return [...m].sort((a,b)=>b[0].localeCompare(a[0])).map(([id,items])=>{items.sort((a,b)=>a.name.localeCompare(b.name));const images=items.filter(a=>a.kind==='image'&&a.thumb);const cover=images[0]||items.find(a=>a.thumb)||items[0];return {id,items,cover,title:names[id]?.[0]||props.groupLabel(id),subtitle:names[id]?.[1]||id.split('-')[0],count:items.length,video:items.filter(a=>a.kind==='video').length}})})
const featured=computed(()=>albums.value.filter(a=>a.cover?.thumb).slice(0,5))
const hero=computed(()=>featured.value.find(a=>a.id===chosen.value)||featured.value[0])
let animation
watch(()=>hero.value?.cover?.thumb,async(src)=>{emit('cover',src||'');await nextTick();animation?.kill();if(!src||window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;const pic=document.querySelector('.archive-hero-picture img'),title=document.querySelector('.archive-hero-content h1');if(pic&&title){animation=gsap.timeline().fromTo(pic,{scale:1.065,opacity:.35},{scale:1,opacity:1,duration:.95,ease:'power3.out'}).fromTo(title,{y:22,opacity:0},{y:0,opacity:1,duration:.52,ease:'power3.out'},.12)}},{immediate:true})
onUnmounted(()=>animation?.kill())
const visible=computed(()=>albums.value.filter(a=>[a.title,a.subtitle,a.id].join(' ').toLowerCase().includes(search.value.toLowerCase())))
const pages=computed(()=>Math.max(1,Math.ceil(visible.value.length/pageSize)))
watch(pages,n=>{page.value=Math.min(page.value,n)})
const rows=computed(()=>visible.value.slice((Math.min(page.value,pages.value)-1)*pageSize,Math.min(page.value,pages.value)*pageSize))
function move(d){if(!featured.value.length)return;const i=featured.value.findIndex(a=>a.id===hero.value?.id);chosen.value=featured.value[(i+d+featured.value.length)%featured.value.length].id}
function go(n){page.value=Math.max(1,Math.min(pages.value,n));document.querySelector('#archive-index')?.scrollIntoView({block:'start'})}
</script>
<template>
<main class="archive-home">
<section v-if="hero" class="archive-hero">
 <div class="archive-hero-picture" :key="hero.id"><img :src="hero.cover.thumb" :alt="hero.title" fetchpriority="high"></div>
 <div class="archive-hero-shade"></div>
 <div class="archive-hero-content"><div class="archive-hero-meta"><span>精选馆藏</span><span>Film {{hero.id.split('-')[0]}}</span></div><h1>{{hero.title}}</h1><p>{{hero.subtitle}} <span class="archive-dot"/> {{hero.count}} 个画面</p><button class="archive-enter" @click="emit('album',hero.id,$event.currentTarget.closest('.archive-hero').querySelector('.archive-hero-picture'))">进入专辑 <ArrowUpRight :size="19"/></button></div>
 <div class="archive-hero-controls"><button aria-label="上一部精选" @click="move(-1)"><ChevronLeft :size="23"/></button><span>{{String(featured.findIndex(a=>a.id===hero.id)+1).padStart(2,'0')}} / {{String(featured.length).padStart(2,'0')}}</span><button aria-label="下一部精选" @click="move(1)"><ChevronRight :size="23"/></button></div>
 <button class="archive-scroll" aria-label="浏览馆藏" @click="go(1)"><ArrowDown :size="20"/></button>
</section>
<section id="archive-index" class="archive-index"><div class="archive-index-head"><div><p class="archive-index-label">馆藏索引 / The collection</p><h2>Selected works</h2></div><div class="archive-index-count">{{albums.length}} 部专辑<br>{{assets.length}} 个画面</div></div>
<div class="archive-index-tools"><label><Search :size="19"/><input id="search" v-model="search" @input="page=1" type="search" placeholder="搜索专辑"><button v-if="search" aria-label="清空搜索" @click="search='';page=1"><X :size="15"/></button></label><button class="archive-all" @click="emit('browse')">浏览所有画面 <ArrowUpRight :size="17"/></button></div>
<div v-if="!rows.length" class="archive-empty">没有匹配的专辑。</div>
<div class="archive-albums"><article v-for="album in rows" :key="album.id" class="archive-album"><button class="archive-cover" :aria-label="'进入专辑 '+album.title" @click="emit('album',album.id,$event.currentTarget)"><img v-if="album.cover.thumb" :src="album.cover.thumb" :alt="album.title" loading="lazy" decoding="async"><span v-else>预览尚未缓存</span><span class="archive-cover-arrow"><ArrowUpRight :size="22"/></span></button><div class="archive-album-caption"><div><h3>{{album.title}}</h3><p>{{album.subtitle}} <span v-if="album.video">/ {{album.video}} 部视频</span></p></div><span>{{String(album.count).padStart(2,'0')}} 帧</span></div></article></div>
<footer class="archive-pages"><span>专辑 {{Math.min(page,pages)}} / {{pages}}</span><div><button aria-label="上一页专辑" :disabled="page<=1" @click="go(page-1)"><ChevronLeft/></button><button aria-label="下一页专辑" :disabled="page>=pages" @click="go(page+1)"><ChevronRight/></button></div></footer>
</section>
<footer class="archive-footer"><span>Digital Film Archive</span><span>本地索引 · 云端原始作品</span></footer>
</main>
</template>
