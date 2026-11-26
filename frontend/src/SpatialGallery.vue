<script setup>
import { computed, ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { ChevronLeft, ChevronRight, RotateCcw } from 'lucide-vue-next'
import { reducedMotion } from './motion.js'
const props=defineProps({assets:Array,mode:{type:String,default:'globe'}})
const emit=defineEmits(['preview'])
const stage=ref(null), project=ref('all'), page=ref(0), yaw=ref(0), pitch=ref(0), width=ref(800), height=ref(540)
const all=computed(()=>props.assets.filter(a=>a.thumb))
const projects=computed(()=>[...new Set(all.value.map(a=>a.project))])
const filtered=computed(()=>all.value.filter(a=>project.value==='all'||a.project===project.value))
const limit=computed(()=>props.mode==='globe'?36:12)
const pages=computed(()=>Math.max(1,Math.ceil(filtered.value.length/limit.value)))
const items=computed(()=>filtered.value.slice(page.value*limit.value,(page.value+1)*limit.value))
const cards=computed(()=>{
 const n=items.value.length, radius=Math.min(width.value*.34,height.value*.34), globe=props.mode==='globe'
 return items.value.map((asset,i)=>{
  let x,y,z
  if(globe){const h=1-2*(i+.5)/n,a=i*Math.PI*(3-Math.sqrt(5))+yaw.value,r=Math.sqrt(1-h*h);x=r*Math.cos(a);const dz=r*Math.sin(a);y=h*Math.cos(pitch.value)-dz*Math.sin(pitch.value);z=h*Math.sin(pitch.value)+dz*Math.cos(pitch.value)}
  else {const a=i*2*Math.PI/n+yaw.value;x=Math.sin(a);y=.12*Math.cos(a);z=Math.cos(a)}
  const d=(z+1)/2,scale=.35+.7*d
  return {asset,i,style:{transform:'translate(-50%,-50%) translate3d('+Math.round(x*radius*(globe?1:1.24))+'px,'+Math.round(y*radius)+'px,'+Math.round(z*radius*.34)+'px) scale('+scale.toFixed(3)+')',zIndex:Math.round(d*1000)+1,opacity:(.18+.82*d).toFixed(3),pointerEvents:z<-.1?'none':'auto'},front:z>=-.1}
 })
})
let pointer=null, velocity=0, frame=0, observer, resize, visible=true, last=0, moved=false
function stop(){cancelAnimationFrame(frame);frame=0}
function down(e){if(e.button!==0)return;stop();pointer={id:e.pointerId,x:e.clientX,y:e.clientY,at:performance.now()};velocity=0;moved=false}
function move(e){if(!pointer||pointer.id!==e.pointerId)return;const dx=e.clientX-pointer.x,dy=e.clientY-pointer.y;if(!moved&&Math.hypot(dx,dy)<5)return;if(!moved){moved=true;stage.value.setPointerCapture(e.pointerId)}const now=performance.now();velocity=Math.max(-.08,Math.min(.08,dx*.008*16/Math.max(8,now-pointer.at)));yaw.value+=dx*.008;if(props.mode==='globe')pitch.value=Math.max(-1.2,Math.min(1.2,pitch.value+dy*.005));pointer={id:e.pointerId,x:e.clientX,y:e.clientY,at:now}}
function up(e){if(pointer?.id!==e.pointerId)return;const recent=performance.now()-pointer.at<100;pointer=null;if(!moved||!recent||reducedMotion()||!visible)return;last=performance.now();const tick=t=>{const dt=Math.min(32,t-last)/16;last=t;velocity*=Math.pow(.9,dt);if(Math.abs(velocity)<.001||!visible||document.hidden){frame=0;return}yaw.value+=velocity*dt;frame=requestAnimationFrame(tick)};frame=requestAnimationFrame(tick)}
function rotate(d){stop();yaw.value+=d*(props.mode==='globe'?.45:2*Math.PI/Math.max(1,items.value.length))}
function reset(){stop();yaw.value=0;pitch.value=0}
function select(a,e){if(moved&&e.detail!==0)return;emit('preview',a,filtered.value)}
function keyboard(e){if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();rotate(e.key==='ArrowLeft'?-1:1)}}
watch(project,()=>{page.value=0;reset()})
watch(pages,n=>{page.value=Math.min(page.value,n-1)})
watch(()=>props.mode,()=>{page.value=0;reset()})
onMounted(()=>{resize=new ResizeObserver(()=>{width.value=stage.value.clientWidth;height.value=stage.value.clientHeight});resize.observe(stage.value);observer=new IntersectionObserver(([e])=>{visible=e.isIntersecting;if(!visible)stop()});observer.observe(stage.value)})
onUnmounted(()=>{stop();resize?.disconnect();observer?.disconnect()})
</script>
<template>
<section class="spatial-gallery">
<div class="spatial-toolbar"><label>专辑 <select v-model="project"><option value="all">全部作品</option><option v-for="p in projects" :key="p" :value="p">{{p}}</option></select></label><div><button aria-label="上一组" :disabled="page===0" @click="page--;reset()"><ChevronLeft :size="18"/></button><span>{{page+1}} / {{pages}}</span><button aria-label="下一组" :disabled="page+1>=pages" @click="page++;reset()"><ChevronRight :size="18"/></button></div></div>
<div ref="stage" class="spatial-stage" :data-spatial-mode="mode" tabindex="0" aria-label="空间画廊，拖动旋转，方向键旋转" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="pointer=null;moved=false;stop()" @lostpointercapture="pointer=null" @keydown="keyboard">
<div class="spatial-ring" aria-hidden="true"/><div class="spatial-ring ring-2" aria-hidden="true"/>
<button v-for="c in cards" :key="c.asset.id" class="spatial-card" :style="c.style" :tabindex="c.front?0:-1" :aria-hidden="!c.front" :aria-label="'查看 '+(c.asset.title||c.asset.name)" @click="select(c.asset,$event)"><img :src="c.asset.thumb" :alt="c.asset.title||c.asset.name" draggable="false"/></button>
<p v-if="!items.length" class="spatial-empty">没有已缓存的预览</p>
</div>
<div class="spatial-footer"><span>拖动旋转 · 点击作品预览 · {{items.length}} / {{filtered.length}} 件</span><div><button aria-label="向左旋转" @click="rotate(-1)"><ChevronLeft :size="19"/></button><button aria-label="重置空间" @click="reset"><RotateCcw :size="17"/></button><button aria-label="向右旋转" @click="rotate(1)"><ChevronRight :size="19"/></button></div></div>
</section>
</template>
