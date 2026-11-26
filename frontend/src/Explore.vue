<script setup>
import { computed, ref, onMounted, onUnmounted, nextTick } from 'vue'
import { Minus, Plus, Scan, Move, ArrowUpRight } from 'lucide-vue-next'
import SpatialGallery from './SpatialGallery.vue'
const mode=ref('canvas')
const props=defineProps({assets:Array})
const emit=defineEmits(['preview'])
const stage=ref(null),camera=ref({x:0,y:0,z:1}),size=ref({w:1000,h:700}),dragging=ref(false)
const reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches
const cellW=300,cellH=235,cols=11
const items=computed(()=>props.assets.filter(a=>a.thumb))
const slots=computed(()=>{const a=items.value;if(!a.length)return [];const c=camera.value;const x0=Math.floor(-c.x/c.z/cellW)-1,x1=Math.ceil((size.value.w-c.x)/c.z/cellW)+1,y0=Math.floor(-c.y/c.z/cellH)-1,y1=Math.ceil((size.value.h-c.y)/c.z/cellH)+1;const out=[];for(let y=y0;y<=y1;y++)for(let x=x0;x<=x1;x++){const n=((y*cols+x)%a.length+a.length)%a.length;const item=a[n];const offset=((x*17+y*23)%3+3)%3;out.push({key:x+':'+y,item,x:x*cellW+18,y:y*cellH+18+offset*8,w:offset===1?250:270})}return out})
let pointer=null,resizeObserver,frame=0,last=0,velocity={x:0,y:0},moved=false
function cancel(){cancelAnimationFrame(frame);frame=0}
function down(e){if(e.button!==0||e.target.closest('.explore-controls'))return;cancel();pointer={id:e.pointerId,x:e.clientX,y:e.clientY,t:performance.now()};moved=false;velocity={x:0,y:0}}
function move(e){if(!pointer||pointer.id!==e.pointerId)return;const dx=e.clientX-pointer.x,dy=e.clientY-pointer.y;if(!moved&&Math.hypot(dx,dy)<4)return;if(!moved){moved=true;dragging.value=true;stage.value.setPointerCapture(e.pointerId)}const now=performance.now(),dt=Math.max(8,now-pointer.t);velocity={x:dx/dt*16,y:dy/dt*16};camera.value={...camera.value,x:camera.value.x+dx,y:camera.value.y+dy};pointer={id:e.pointerId,x:e.clientX,y:e.clientY,t:now}}
function up(e){if(!pointer||pointer.id!==e.pointerId)return;const recent=performance.now()-pointer.t<100;pointer=null;dragging.value=false;if(moved&&recent&&!reduced){last=performance.now();const tick=t=>{const dt=Math.min(32,t-last)/16;last=t;velocity.x*=Math.pow(.89,dt);velocity.y*=Math.pow(.89,dt);if(Math.hypot(velocity.x,velocity.y)<.35){frame=0;return}camera.value={...camera.value,x:camera.value.x+velocity.x*dt,y:camera.value.y+velocity.y*dt};frame=requestAnimationFrame(tick)};frame=requestAnimationFrame(tick)}}
function zoom(f,x=size.value.w/2,y=size.value.h/2){cancel();const c=camera.value,z=Math.max(.45,Math.min(2.4,c.z*f));camera.value={x:x-(x-c.x)*z/c.z,y:y-(y-c.y)*z/c.z,z}}
function wheel(e){e.preventDefault();const r=stage.value.getBoundingClientRect();zoom(Math.exp(-e.deltaY*.0015),e.clientX-r.left,e.clientY-r.top)}
function reset(){cancel();camera.value={x:size.value.w/2-150,y:size.value.h/2-115,z:1}}
function choose(slot,e){if(moved&&e.detail!==0)return;emit('preview',slot.item,items.value)}
function key(e){if(e.target.closest('button'))return;const d=90;const dirs={ArrowLeft:[d,0],ArrowRight:[-d,0],ArrowUp:[0,d],ArrowDown:[0,-d]};if(dirs[e.key]){e.preventDefault();const [x,y]=dirs[e.key];camera.value={...camera.value,x:camera.value.x+x,y:camera.value.y+y}}else if(e.key==='+'||e.key==='=')zoom(1.2);else if(e.key==='-')zoom(1/1.2)}
function attachCanvas(){if(!stage.value)return;resizeObserver?.disconnect();resizeObserver=new ResizeObserver(()=>{size.value={w:stage.value.clientWidth,h:stage.value.clientHeight}});resizeObserver.observe(stage.value);stage.value.addEventListener('wheel',wheel,{passive:false});size.value={w:stage.value.clientWidth,h:stage.value.clientHeight};reset()}
onMounted(attachCanvas)
function changeMode(m){cancel();resizeObserver?.disconnect();stage.value?.removeEventListener('wheel',wheel);mode.value=m;if(m==='canvas')nextTick(attachCanvas)}
onUnmounted(()=>{cancel();resizeObserver?.disconnect();stage.value?.removeEventListener('wheel',wheel)})
</script>
<template>
<main class="explore-page">
 <div class="explore-top"><div><p>自由画布 / Explore</p><h1>Every frame,<br>another world.</h1></div><span>{{items.length}} 个可探索画面</span></div>
 <div class="explore-modes"><button v-for="[id,label] in [['canvas','无限画布'],['globe','作品星球'],['orbit','轨道画廊']]" :key="id" :aria-pressed="mode===id" @click="changeMode(id)">{{label}}</button></div>
 <SpatialGallery v-if="mode!=='canvas'" :mode="mode" :assets="assets" @preview="(a,list)=>emit('preview',a,list)"/>
 <section v-else ref="stage" class="explore-stage" :class="{dragging}" tabindex="0" aria-label="作品探索画布，拖拽移动，滚轮缩放，方向键移动" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="pointer=null;dragging=false;cancel()" @lostpointercapture="pointer=null" @keydown="key">
 <div v-if="!items.length" class="explore-empty">没有已缓存的作品预览。</div>
 <div class="explore-world" :style="{transform:'translate('+camera.x+'px,'+camera.y+'px) scale('+camera.z+')'}">
 <button v-for="slot in slots" :key="slot.key" class="explore-frame" :style="{left:slot.x+'px',top:slot.y+'px',width:slot.w+'px'}" :aria-label="'查看 '+(slot.item.title||slot.item.name)" @click="choose(slot,$event)"><img :src="slot.item.thumb" :alt="slot.item.title||slot.item.name" draggable="false"><span>{{slot.item.title||slot.item.name}} <ArrowUpRight :size="13"/></span></button>
 </div>
 <div class="explore-controls"><button aria-label="缩小" @click="zoom(1/1.2)"><Minus :size="20"/></button><span>{{Math.round(camera.z*100)}}%</span><button aria-label="放大" @click="zoom(1.2)"><Plus :size="20"/></button><button aria-label="重置画布" @click="reset"><Scan :size="19"/></button></div>
 <div class="explore-hint"><Move :size="17"/> 拖拽探索 · 滚轮缩放 · 点击预览</div>
 </section>
</main>
</template>
