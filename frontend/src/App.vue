<script setup>
import { computed, ref, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { Aperture, LayoutGrid, Images, Clapperboard, Search, ChevronLeft, ChevronRight, ArrowUpRight, SlidersHorizontal, Sun, Moon, Activity, Play, Maximize2, Check, X, ExternalLink, FolderOpen, Image as ImageIcon } from 'lucide-vue-next'
import Monitor from './Monitor.vue'
import ArchiveHome from './ArchiveHome.vue'
import Explore from './Explore.vue'
import { gsap } from 'gsap'
import { motion, reducedMotion } from './motion.js'
import ArtistPalette from './ArtistPalette.vue'
import { themes } from './artist-palettes.js'
import { livingColors } from './living-colors.js'
import MediaViewer from './MediaViewer.vue'
import Atelier from './Atelier.vue'
import { useCatalogSync } from './useCatalogSync.js'
import { galleryPreview, canFallbackToOriginal } from './catalog.js'
const atelierSource=ref(null)
function createFrom(a){atelierSource.value=a;close();navigate('atelier')}
const props=defineProps({initial:Object})
const fontOptions=[
 {id:'dynapuff',name:'DynaPuff',family:'DynaPuff Variable'},
 {id:'cherry',name:'Cherry Bomb One',family:'Cherry Bomb One'},
 {id:'bagel',name:'Bagel Fat One',family:'Bagel Fat One'},
 {id:'modak',name:'Modak',family:'Modak'},
 {id:'bricolage',name:'Bricolage Grotesque',family:'Bricolage Grotesque Variable'},
 {id:'shrikhand',name:'Shrikhand',family:'Shrikhand'},
 {id:'fraunces',name:'Fraunces',family:'Fraunces Variable'},
 {id:'kablammo',name:'Kablammo',family:'Kablammo Variable'}
]
function savedFont(){try{const v=localStorage.getItem('dfa.displayFont.v1');return fontOptions.some(f=>f.id===v)?v:'dynapuff'}catch{return 'dynapuff'}}
const displayFont=ref(savedFont()),fontPanel=ref(false)
const activeFont=computed(()=>fontOptions.find(f=>f.id===displayFont.value)||fontOptions[0])
function chooseFont(id){displayFont.value=id;try{localStorage.setItem('dfa.displayFont.v1',id)}catch{}}

const assets=ref(props.initial.assets||[]), tasks=ref(props.initial.tasks||[])
const view=ref(['monitor','atelier'].includes(props.initial.view)?props.initial.view:'archive'), project=ref('all'), kind=ref('all'), query=ref(''), sort=ref('new'), layout=ref('masonry'), page=ref(1), pageSize=ref(12), online=ref(false), selected=ref(props.initial.selected||tasks.value[0]?.id||''), focus=ref(0)
function savedPalette(){try{const id=localStorage.getItem('dfa.artistPalette.v1');return themes.some(t=>t.id===id)?id:'neo'}catch{return 'neo'}}
const artistTheme=ref(savedPalette()),palettePanel=ref(false)
const currentPalette=computed(()=>themes.find(t=>t.id===artistTheme.value)||themes[0])
function choosePalette(id){if(!themes.some(t=>t.id===id))return;artistTheme.value=id;try{localStorage.setItem('dfa.artistPalette.v1',id)}catch{}}
watch(fontPanel,v=>{if(v)palettePanel.value=false})
watch(palettePanel,v=>{if(v)fontPanel.value=false})
const viewer=ref(null),viewerItems=ref([]),viewerIndex=ref(0);let lastFocus
const groupLabel=(x)=>x==='015-isolated'?'Before rain stops · 24 shots':x.replace(/^\d+-/,'').split('-').map((w,i)=>w==='minimax'?'MiniMax':w==='moss'?'MOSS':w==='pika'?'Pika':i===0?w[0]?.toUpperCase()+w.slice(1):w).join(' ')
const groupList=computed(()=>{const m=new Map();for(const a of assets.value)m.set(a.project,(m.get(a.project)||0)+1);return [...m].sort((a,b)=>b[0].localeCompare(a[0]))})
const filter=computed(()=>assets.value.filter(a=>(project.value==='all'||a.project===project.value)&&(kind.value==='all'||a.kind===kind.value)&&(!query.value.trim()||[a.name,a.title,a.project,a.prompt].filter(Boolean).join(' ').toLowerCase().includes(query.value.trim().toLowerCase()))).sort((a,b)=>sort.value==='size'?(b.bytes||0)-(a.bytes||0):sort.value==='old'?a.project.localeCompare(b.project)||a.name.localeCompare(b.name):b.project.localeCompare(a.project)||a.name.localeCompare(b.name)))
const pageCount=computed(()=>Math.max(1,Math.ceil(filter.value.length/pageSize.value)))
const current=computed(()=>filter.value.slice((page.value-1)*pageSize.value,page.value*pageSize.value))
const feature=computed(()=>current.value.find(a=>a.kind==='image'&&a.thumb)||filter.value.find(a=>a.kind==='image'&&a.thumb))
const showFeature=computed(()=>view.value==='gallery'&&layout.value==='masonry'&&page.value===1&&!query.value&&!kind.value.includes('video')&&!!feature.value)
const film=computed(()=>current.value[focus.value]||current.value[0])
const activeTask=computed(()=>tasks.value.find(t=>t.id===selected.value))
const thumbs=computed(()=>Object.fromEntries(assets.value.flatMap(a=>a.thumb?[[a.url,a.thumb],...(a.local_url?[[a.local_url,a.thumb]]:[])]:[])))
const mediaIndex=computed(()=>Object.fromEntries(assets.value.flatMap(a=>[[a.id,a],[a.url,a],...(a.local_url?[[a.local_url,a]]:[])])))
const canonical=computed(()=>Object.fromEntries(assets.value.filter(a=>a.local_url&&a.url).map(a=>[a.local_url,a.url])))
const liveTask=computed(()=>activeTask.value&&({ ...activeTask.value, snapshot:normalize(activeTask.value.snapshot) }))
const bytes=n=>n==null?'':n>=1048576?(n/1048576).toFixed(1)+' MB':Math.round(n/1024)+' KB'
const imageCount=computed(()=>assets.value.filter(a=>a.kind==='image').length)
const videoCount=computed(()=>assets.value.length-imageCount.value)
const counts=computed(()=>({running:tasks.value.filter(t=>t.snapshot?.status==='running').length,done:tasks.value.filter(t=>t.snapshot?.status==='completed').length}))
function normalize(s){if(!s)return s;return {...s,jobs:(s.jobs||[]).map(j=>j.asset&&canonical.value[j.asset.url]?{...j,asset:{...j.asset,url:canonical.value[j.asset.url]}}:j)}}
const { updateTask }=useCatalogSync(props.initial,assets,tasks,selected)
watch([project,kind,query,sort,pageSize,layout],()=>{page.value=1;focus.value=0})
watch(pageCount,n=>{page.value=Math.min(page.value,n)})
watch(page,()=>{focus.value=0})
watch(currentPalette,t=>{
 const root=document.documentElement
 root.dataset.theme=t.dark?'dark':'light'
 for(const k of ['paper','surface','ink','muted','line','accent','soft'])root.style.setProperty('--'+k,t[k])
 root.style.colorScheme=t.dark?'dark':'light'
},{immediate:true})
function setPage(n){page.value=Math.max(1,Math.min(pageCount.value,n));document.querySelector('#collection')?.scrollIntoView({block:'start',behavior:'instant'})}
function open(a,items=filter.value){if(!a)return;const i=items.findIndex(x=>x.id===a.id);if(i<0)return;lastFocus=document.activeElement;viewerItems.value=items;viewerIndex.value=i;viewer.value=a}
function close(){viewer.value=null;nextTick(()=>lastFocus?.focus())}
function step(delta){const n=viewerIndex.value+delta;if(n<0||n>=viewerItems.value.length)return;viewerIndex.value=n;viewer.value=viewerItems.value[n]}
const productionCovers=computed(()=>{const map={};for(const a of assets.value){if(a.thumb&&!map[a.project])map[a.project]=a.thumb}return map})
const activeStatuses=['running','queued','delivering']
const productionGroups=computed(()=>[
 {id:'active',label:'制作中',items:tasks.value.filter(t=>activeStatuses.includes(t.snapshot.status))},
 {id:'done',label:'已完成',items:tasks.value.filter(t=>t.snapshot.status==='completed')},
 {id:'other',label:'其他记录',items:tasks.value.filter(t=>!activeStatuses.includes(t.snapshot.status)&&t.snapshot.status!=='completed')}
].filter(g=>g.items.length))
const railSearch=ref('')
const railGroups=computed(()=>productionGroups.value.map(g=>({...g,items:g.items.filter(t=>!railSearch.value.trim()||t.snapshot.name.toLowerCase().includes(railSearch.value.trim().toLowerCase())||groupLabel(t.snapshot.name).toLowerCase().includes(railSearch.value.trim().toLowerCase()))})).filter(g=>g.items.length))
function showTask(id){selected.value=id;view.value='monitor';window.scrollTo({top:0,behavior:'instant'})}
function taskForProject(){const t=tasks.value.find(t=>t.snapshot?.name===project.value);if(t)showTask(t.id)}
const projectTask=computed(()=>tasks.value.find(t=>t.snapshot?.name===project.value))
function navigate(v){fontPanel.value=false;palettePanel.value=false;view.value=v;if(v==='monitor'&&!selected.value)selected.value=tasks.value[0]?.id||'';window.scrollTo({top:0,behavior:'instant'})}
const paletteStyle=computed(()=>{const t=currentPalette.value;return {...livingColors(t),...Object.fromEntries([...['paper','surface','ink','muted','line','accent','soft','pop'].flatMap(k=>[['--artist-'+k,t[k]],['--'+k,t[k]]]),['--artist-scheme',t.dark?'dark':'light'],['--artist-link',t.link||t.accent],['--artist-success',t.success||t.accent],['--artist-warning',t.warning||t.accent],['--artist-danger',t.danger||'#b44451'],['--artist-stage',t.stage||'#15151c'],['backgroundColor',t.paper],['color',t.ink]])}})
const reduced=reducedMotion
let routeAnimation=null
async function transitionAlbum(id, source){if(reduced()||!source?.querySelector('img')){openAlbum(id);return}
 const img=source.querySelector('img'),box=img.getBoundingClientRect(),ghost=document.createElement('img')
 ghost.src=img.src;ghost.className='transition-frame';Object.assign(ghost.style,{left:box.left+'px',top:box.top+'px',width:box.width+'px',height:box.height+'px'});document.body.appendChild(ghost)
 routeAnimation?.kill();document.querySelectorAll('.transition-frame').forEach(el=>{if(el!==ghost)el.remove()})
 openAlbum(id);await nextTick();const target=document.querySelector('.film-stage');if(!target){ghost.remove();return}
 const dest=target.getBoundingClientRect();routeAnimation=gsap.timeline({onComplete:()=>{ghost.remove();routeAnimation=null}}).to(ghost,{left:dest.left,top:dest.top,width:dest.width,height:dest.height,duration:motion.medium,ease:motion.routeEase}).to(ghost,{opacity:0,duration:motion.short,ease:motion.ease})
}
function openAlbum(id){project.value=id;kind.value='all';query.value='';layout.value='film';navigate('gallery')}
function browseAll(){project.value='all';kind.value='all';query.value='';layout.value='grid';navigate('gallery')}
function previewSrc(a){return galleryPreview(a,online.value)}
function previewError(e,a){const img=e.currentTarget;if(canFallbackToOriginal(a,online.value)&&img.getAttribute('src')!==a.url){img.src=a.url}else{img.alt='图片暂时无法加载'}}
function previewStyle(a){const dims=a.dimensions;return dims?.[0]&&dims?.[1]?{aspectRatio:dims[0]+' / '+dims[1]}:{}}
function pages(){return [...new Set([1,...[page.value-1,page.value,page.value+1].filter(n=>n>0&&n<=pageCount.value),pageCount.value])].sort((a,b)=>a-b)}
function keyboard(e){if(viewer.value||e.defaultPrevented||e.target?.closest?.('input,textarea,select,[contenteditable]'))return;if(view.value==='gallery'&&layout.value==='film'&&['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();focus.value=Math.max(0,Math.min(current.value.length-1,focus.value+(e.key==='ArrowLeft'?-1:1)));return}if(e.key==='/'&&!e.target?.closest?.('button')){e.preventDefault();document.querySelector('#search')?.focus()}}
onMounted(()=>{document.documentElement.dataset.theme=currentPalette.value.dark?'dark':'light';document.addEventListener('keydown',keyboard)})
onUnmounted(()=>{routeAnimation?.kill();document.querySelectorAll('.transition-frame').forEach(el=>el.remove());document.removeEventListener('keydown',keyboard)})
</script>
<template>
<div class="studio creative" :data-display-font="displayFont" :data-palette="artistTheme" :data-view="view" :style="paletteStyle">
<div v-if="view!=='monitor'" class="living-atmosphere" aria-hidden="true"><i/><i/><i/></div><header class="topbar"><button class="wordmark" @click="navigate('archive')"><span>Digital Film Archive<span class="archive-brand-sub">数字电影档案馆</span></span></button><nav class="main-nav" aria-label="主导航"><button :class="{active:view==='atelier'}" @click="navigate('atelier')">创作</button><button :class="{active:view==='archive'}" @click="navigate('archive')">馆藏</button><button :class="{active:view==='gallery'}" @click="browseAll">所有画面</button><button :class="{active:view==='explore'}" @click="navigate('explore')">探索</button><button :class="{active:view==='monitor'}" @click="navigate('monitor')"><Activity :size="17"/> 任务监控 <span v-if="counts.running" class="nav-count">{{counts.running}}</span></button></nav><div class="top-actions"><div class="font-switch" @keydown.esc.stop="fontPanel=false"><button class="font-toggle" aria-label="艺术字体切换" :aria-expanded="fontPanel" aria-controls="art-font-options" @click="fontPanel=!fontPanel"><span class="font-symbol">Aa</span><span class="font-current">{{activeFont.name}}</span></button><section v-if="fontPanel" id="art-font-options" class="font-panel" aria-label="艺术字体"><div class="font-panel-head"><div><strong>Typography</strong><small>实时切换艺术字体</small></div><button aria-label="关闭字体面板" @click="fontPanel=false"><X :size="17"/></button></div><div class="font-options"><button v-for="f in fontOptions" :key="f.id" :data-font-option="f.id" :aria-pressed="displayFont===f.id" @click="chooseFont(f.id)"><span class="font-sample">Dreams</span><span class="font-option-name">{{f.name}}</span><Check v-if="displayFont===f.id" :size="15"/></button></div><button class="font-reset" @click="chooseFont('dynapuff')">恢复默认 DynaPuff</button></section></div><a v-if="initial.bucket_url" class="cloud-link" :href="initial.bucket_url" target="_blank" rel="noopener noreferrer"><span class="dot"/> HF 存储 <ArrowUpRight :size="14"/></a><ArtistPalette :themes="themes" :model-value="artistTheme" :open="palettePanel" @update:model-value="choosePalette" @update:open="palettePanel=$event"/></div></header>
<div class="shell" :class="{'archive-shell':view!=='monitor','production-shell':view==='monitor'}">
<ArchiveHome v-if="view==='archive'" :assets="assets" :group-label="groupLabel" @album="transitionAlbum" @browse="browseAll"/>
<Atelier v-if="view==='atelier'" :assets="assets" :source="atelierSource" :live="!!initial.live_studio" :catalog="initial.image_workflows" @browse="browseAll" @preview="a=>open(a,assets)"/>
<Explore v-if="view==='explore'" :assets="assets" @preview="open"/>
<aside v-if="view==='monitor'" class="sidebar production-rail" aria-label="制作档案目录">
  <div class="production-rail-title"><h2>Productions</h2><p>{{tasks.length}} 个任务 <span>·</span> {{counts.running}} 运行中 <span>·</span> {{counts.done}} 已完成</p></div>
  <label class="production-rail-search"><Search :size="15"/><input v-model="railSearch" placeholder="查找制作档案" aria-label="查找制作档案"><button v-if="railSearch" @click="railSearch=''" aria-label="清除搜索"><X :size="14"/></button></label>
  <div class="production-rail-scroll">
    <section v-for="g in railGroups" :key="g.id" class="production-rail-group"><h3>{{g.label}} <span>{{g.items.length}}</span></h3><div class="rail-items">
      <button v-for="t in g.items" :key="t.id" class="task-link" :class="{active:selected===t.id}" :aria-current="selected===t.id?'true':undefined" :title="groupLabel(t.snapshot.name)" @click="showTask(t.id)">
        <span class="rail-cover"><img v-if="productionCovers[t.snapshot.name]" :src="productionCovers[t.snapshot.name]" alt="" loading="lazy"><span v-else>{{t.snapshot.name.split('-')[0]}}</span></span>
        <span class="production-task-name"><strong>{{groupLabel(t.snapshot.name)}}</strong><span class="rail-meta"><i class="task-dot" :data-status="t.snapshot.status"/><span>{{t.snapshot.name.split('-')[0]}}</span><span class="rail-progress">{{t.snapshot.counts?.completed||0}} / {{t.snapshot.total??'—'}}</span></span></span>
      </button></div>
    </section>
    <p v-if="!railGroups.length" class="rail-empty">没有匹配的制作档案</p>
  </div>
</aside>
<main v-if="view==='gallery'" class="workspace archive-browse">
  <button class="archive-back" @click="navigate('archive')"><ChevronLeft :size="17"/> 返回馆藏</button>
  <div class="gallery-heading"><div><div class="breadcrumb">作品库 <template v-if="project!=='all'">/ {{project.split('-')[0]}}</template></div><h1>{{project==='all'?'你的作品集':groupLabel(project)}}</h1><p>{{assets.length}} 件作品 <span class="separate"/> {{imageCount}} 张图片 <span class="separate"/> {{videoCount}} 部视频</p></div><button v-if="projectTask" class="text-btn" @click="taskForProject"><Activity :size="17"/> 查看任务记录 <ArrowUpRight :size="15"/></button></div>

  <section id="collection" class="collection"><div class="collection-title"><h2>{{project==='all'?'全部作品':'项目作品'}}</h2><span>{{filter.length}} 件</span></div>
  <div class="toolbox"><label class="search"><Search :size="18"/><input id="search" v-model="query" placeholder="搜索作品、项目、提示词" type="search"><kbd>/</kbd></label><div class="filter-tabs" aria-label="媒体类型"><button v-for="[value,label] in [['all','全部'],['image','图片'],['video','视频']]" :key="value" :class="{active:kind===value}" @click="kind=value">{{label}}</button></div><div class="layout-switch" aria-label="布局"><button aria-label="瀑布流" title="瀑布流" :aria-pressed="layout==='masonry'" :class="{active:layout==='masonry'}" @click="layout='masonry'"><Images :size="17"/></button><button aria-label="联系表" title="联系表" :aria-pressed="layout==='grid'" :class="{active:layout==='grid'}" @click="layout='grid'"><LayoutGrid :size="17"/></button><button aria-label="电影放映" title="电影放映" :aria-pressed="layout==='film'" :class="{active:layout==='film'}" @click="layout='film'"><Clapperboard :size="18"/></button></div><label class="select"><SlidersHorizontal :size="16"/><select v-model="sort" aria-label="排序"><option value="new">项目倒序</option><option value="old">项目正序</option><option value="size">文件大小</option></select></label></div>
  <div class="collection-meta"><label class="check"><input v-model="online" type="checkbox"> 在线加载缺失预览</label><div class="pagination-mini"><span>{{filter.length?(page-1)*pageSize+1:0}}–{{Math.min(page*pageSize,filter.length)}} / {{filter.length}}</span><button class="icon-btn" aria-label="上一页" :disabled="page<=1" @click="setPage(page-1)"><ChevronLeft :size="18"/></button><button class="icon-btn" aria-label="下一页" :disabled="page>=pageCount" @click="setPage(page+1)"><ChevronRight :size="18"/></button></div></div>
  <div v-if="!current.length" class="empty"><Aperture :size="36" :stroke-width="1"/><h2>没有匹配的作品</h2><button @click="project='all';query='';kind='all'">清除筛选</button></div>
  <div v-else-if="layout==='film'" class="film"><button class="film-stage" @click="open(film)"><img v-if="previewSrc(film)" :src="previewSrc(film)" @error="previewError($event,film)" alt=""><span v-else>{{film.kind==='video'?'视频':'图片'}} · 点击查看</span><span v-if="film.kind==='video'" class="play-icon"><Play :size="26" fill="currentColor"/></span></button><div class="film-caption"><div><h3>{{film.title||film.name}}</h3><small>{{groupLabel(film.project)}}</small></div><div class="actions"><button :disabled="focus<=0" aria-label="上一镜头" @click="focus--"><ChevronLeft :size="18"/></button><button :disabled="focus>=current.length-1" aria-label="下一镜头" @click="focus++"><ChevronRight :size="18"/></button><button @click="open(film)">查看作品 <ArrowUpRight :size="15"/></button></div></div><div class="filmstrip"><button v-for="(a,i) in current" :key="a.id" :class="{active:i===focus}" :aria-label="a.name" @click="focus=i"><img v-if="previewSrc(a)" :src="previewSrc(a)" @error="previewError($event,a)" alt=""><span v-else>{{a.name}}</span><small>{{String(i+1).padStart(2,'0')}}</small></button></div></div>
  <div v-else class="gallery-grid" :class="layout"><article v-for="a in current" :key="a.id" class="tile"><button class="tile-media" :style="layout==='masonry'?previewStyle(a):{}" :aria-label="'查看 '+a.name" @click="open(a)"><img v-if="previewSrc(a)" :src="previewSrc(a)" @error="previewError($event,a)" :alt="a.title||a.name" loading="lazy" decoding="async"><span v-else class="placeholder"><component :is="a.kind==='video'?Clapperboard:ImageIcon" :size="29" :stroke-width="1.3"/> 点击查看{{a.kind==='video'?'视频':'图片'}}</span><span v-if="a.kind==='video'" class="play-icon"><Play :size="18" fill="currentColor"/></span><span class="tile-expand"><Maximize2 :size="16"/></span></button><div class="tile-caption"><div><h3>{{a.title||a.name}}</h3><p>{{groupLabel(a.project)}}</p></div><small>{{a.dimensions?a.dimensions.join('×'):bytes(a.bytes)}}<span class="storage-tag">{{a.storage==='cloud'?'HF':'LOCAL'}}</span></small></div></article></div>
  <footer class="page-footer"><span>第 {{page}} / {{pageCount}} 页</span><div class="page-buttons"><button aria-label="上一页" :disabled="page===1" @click="setPage(page-1)"><ChevronLeft :size="17"/></button><template v-for="(n,i) in pages()" :key="n"><span v-if="i>0&&n-pages()[i-1]>1">…</span><button :aria-current="page===n?'page':undefined" @click="setPage(n)">{{n}}</button></template><button aria-label="下一页" :disabled="page===pageCount" @click="setPage(page+1)"><ChevronRight :size="17"/></button></div><select v-model.number="pageSize" aria-label="每页数量"><option :value="12">12 / 页</option><option :value="24">24 / 页</option><option :value="48">48 / 页</option></select></footer>
  </section>
</main>
<main v-else-if="view==='monitor'" class="workspace production-workspace"><div v-if="!activeTask" class="empty">暂无任务记录</div><Monitor v-else :key="activeTask.id" :task="liveTask" :thumbs="thumbs" :canonical="canonical" :media-index="mediaIndex" @preview="open" @update="updateTask"/></main>
</div>
<MediaViewer v-if="viewer" :asset="viewer" :position="viewerIndex" :total="viewerItems.length" @close="close" @step="step" @create="createFrom"/>
</div>
</template>
