<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { monitorAsset } from './catalog.js'
import { RefreshCw, ExternalLink, CheckCircle2, Clock3, AlertCircle, Activity, Images, ChevronDown, Search, LayoutGrid, List } from 'lucide-vue-next'
const props=defineProps({ task:Object, thumbs:Object, canonical:Object, mediaIndex:Object })
const emit=defineEmits(['preview','update'])
const live=ref(null),auto=ref(true),connection=ref('已保存的运行记录'),loading=ref(false)
let timer, script, timeout, callback, sequence=0
const task=computed(()=>{const s=live.value||props.task?.snapshot||{};return {...s,jobs:(s.jobs||[]).map(j=>j.asset&&props.canonical?.[j.asset.url]?{...j,asset:{...j.asset,url:props.canonical[j.asset.url]}}:j)}})
const jobs=computed(()=>task.value.jobs||[])
const byKey=computed(()=>Object.fromEntries(jobs.value.map(j=>[j.key,j])))
const count=computed(()=>task.value.counts||{})
const progress=computed(()=>Math.min(100,Math.max(0,Math.round((count.value.completed||0)/Math.max(task.value.total||0,1)*100))))
const title=computed(()=>(task.value.name||'').replace(/^\d+-/,'').replace(/-\d+(?:shots|scenes)?$/,'').replaceAll('-',' ').replace(/\b(moss|minimax|pika)\b/gi,v=>v.toLowerCase()==='minimax'?'MiniMax':v.toLowerCase()==='moss'?'MOSS':'Pika'))
const shotFilter=ref('all'), shotSearch=ref(''), compact=ref(false)
const shotRows=computed(()=>(task.value.shots?.length?task.value.shots:Object.values(jobs.value.reduce((m,j)=>{(m[j.item]??={item:j.item,keys:[]}).keys.push(j.key);return m},{}))).map(shot=>{const rows=shot.keys.map(k=>byKey.value[k]).filter(Boolean);return {...shot,rows,done:rows.filter(j=>j.status==='completed').length,failed:rows.some(j=>j.status==='failed'),active:rows.some(j=>['running','queued'].includes(j.status))}}))
const visibleShots=computed(()=>shotRows.value.filter(s=>(shotFilter.value==='all'||(shotFilter.value==='completed'&&s.rows.length&&s.done===s.rows.length)||(shotFilter.value==='failed'&&s.failed)||(shotFilter.value==='pending'&&s.done<s.rows.length&&!s.failed))&&(!shotSearch.value.trim()||[s.item,...s.rows.map(j=>j.asset?.filename||j.step)].join(' ').toLowerCase().includes(shotSearch.value.trim().toLowerCase()))))
const filters=computed(()=>[{id:'all',label:'全部',n:shotRows.value.length},{id:'completed',label:'已完成',n:shotRows.value.filter(s=>s.rows.length&&s.done===s.rows.length).length},{id:'pending',label:'待完成',n:shotRows.value.filter(s=>s.done<s.rows.length&&!s.failed).length},{id:'failed',label:'失败',n:shotRows.value.filter(s=>s.failed).length}])
const stills=computed(()=>jobs.value.filter(j=>j.asset?.thumb||props.thumbs?.[j.asset?.url]).slice(0,3))
const shotState=s=>s.failed?'failed':s.rows.length&&s.done===s.rows.length?'completed':s.active?'running':'pending'
const shotStateLabel=s=>({failed:'失败',completed:'已完成',running:'进行中',pending:'待完成'}[shotState(s)])

const time=ms=>ms==null?'未采集':ms<1000?Math.round(ms)+' ms':ms<60000?(ms/1000).toFixed(1)+' s':Math.floor(ms/60000)+'m '+Math.floor(ms%60000/1000)+'s'
const date=t=>t==null?'—':new Date(typeof t==='number'?t*1000:t).toLocaleString('zh-CN',{hour12:false})
const bytes=n=>n==null?'未采集':(n/1048576).toFixed(2)+' MiB'
const stepNames={first:'首帧',last:'尾帧',video:'视频',image:'图片'}
function stop(){clearTimeout(timeout);script?.remove();script=null;loading.value=false;sequence++}
function refresh(){if(!props.task?.feed||loading.value)return;const id=++sequence;loading.value=true;connection.value='读取运行记录…'
 const expected=props.task.snapshot.name
 callback=data=>{if(id!==sequence||data.name!==expected)return;live.value=data;emit('update',data);connection.value='已更新 '+new Date().toLocaleTimeString('zh-CN');stop()}
 window.updateTaskMonitor=callback;script=document.createElement('script');script.src=props.task.feed+'?t='+Date.now();script.onerror=()=>{if(id===sequence){connection.value='本地运行记录暂不可读';stop()}};script.onload=()=>{if(id===sequence){connection.value='未收到更新';stop()}};document.body.appendChild(script)
 timeout=setTimeout(()=>{if(id===sequence){connection.value='读取超时';stop()}},8000)
}
watch(()=>props.task?.id,()=>{stop();live.value=null;connection.value='已保存的运行记录';refresh()},{immediate:true})
watch(auto,()=>{clearInterval(timer);if(auto.value)timer=setInterval(refresh,5000)},{immediate:true})
onUnmounted(()=>{clearInterval(timer);stop();if(window.updateTaskMonitor===callback)window.updateTaskMonitor=()=>{}})
function assetFor(j){return monitorAsset(j,task.value.name,props.mediaIndex,props.thumbs)}
function fallback(e,a){if(a.kind==='image'&&e.currentTarget.getAttribute('src')!==a.url)e.currentTarget.src=a.url}
const previewItems=computed(()=>visibleShots.value.flatMap(s=>s.rows.map(assetFor).filter(Boolean)))
function preview(j){const a=assetFor(j);if(!a)return;const list=previewItems.value.some(x=>x.id===a.id)?previewItems.value:shotRows.value.flatMap(s=>s.rows.map(assetFor).filter(Boolean));emit('preview',a,list)}
</script>
<template>
<section v-if="props.task" class="monitor">
  <header class="monitor-heading">
    <div class="monitor-heading-copy"><div class="breadcrumb">制作档案 <span>/</span> {{task.name?.split('-')[0]}}</div><h1>{{title}}</h1><div class="heading-status"><span class="status" :data-status="task.status"><i/>{{task.status_label||task.status}}</span><span class="monitor-id">{{task.name}}</span></div></div>
    <div class="actions"><label class="check"><input v-model="auto" type="checkbox"> 自动更新</label><button :disabled="loading||!props.task.feed" @click="refresh" title="刷新运行记录"><RefreshCw :size="16" :class="{spin:loading}"/> 刷新</button></div>
  </header>
  <div class="production-overview">
    <div class="production-summary">
      <div class="production-progress-title"><div><span>制作进度</span><small>{{count.completed||0}} / {{task.total||0}} 个生成步骤</small></div><b>{{progress}}<em>%</em></b></div>
      <div class="progress-track" role="progressbar" aria-label="生成进度" :aria-valuenow="progress" aria-valuemin="0" aria-valuemax="100"><div :style="{width:progress+'%'}"/></div>
      <div class="monitor-metrics"><div><small>媒体可用</small><b>{{task.downloaded||0}}</b></div><div><small>失败步骤</small><b :class="{'metric-danger':count.failed}">{{count.failed||0}}</b></div><div><small>实测耗时</small><b>{{time(task.elapsed_ms)}}</b></div></div>
    </div>
    <div v-if="stills.length" class="production-stills" aria-label="已生成的代表画面"><button v-for="j in stills" :key="j.key" class="production-still" @click="preview(j)"><img :src="thumbs[j.asset.url]||j.asset.thumb" :alt="'镜头 '+j.item"><span>{{j.item}}</span></button></div>
    <div v-else class="production-reel"><div class="reel-heading"><span>镜头序列</span><span>{{shotRows.length}} 帧组</span></div><div class="reel-frames"><button v-for="s in shotRows" :key="s.item" :data-status="shotState(s)" :title="'镜头 '+s.item+' · '+shotStateLabel(s)" @click="shotSearch=String(s.item);shotFilter='all'">{{s.item}}</button></div><p>画面生成后将自动显示于此。</p></div>
  </div>
  <div class="monitor-meta"><span>开始 {{date(task.started_at)}}</span><span>结束 {{date(task.finished_at)}}</span><span role="status" class="connection"><i/>{{connection}}</span><a v-if="task.log_url" :href="task.log_url" target="_blank" rel="noopener">事件日志 <ExternalLink :size="13"/></a></div>
  <details class="fold"><summary>任务阶段累计耗时 <small>并行阶段不能直接相加</small></summary><table><thead><tr><th>阶段</th><th>耗时</th><th>状态</th><th>来源</th></tr></thead><tbody><tr v-for="s in task.stages||[]" :key="s.stage"><td>{{s.label||s.stage}}</td><td>{{time(s.duration_ms)}}</td><td>{{s.status}}</td><td>{{s.source}}</td></tr></tbody></table></details>
  <div class="production-section-title"><div><h2>Contact sheet <span>镜头目录</span></h2><p>{{visibleShots.length}} / {{shotRows.length}} 个镜头</p></div><div class="monitor-layout" aria-label="显示方式"><button :aria-pressed="!compact" @click="compact=false" title="网格"><LayoutGrid :size="17"/></button><button :aria-pressed="compact" @click="compact=true" title="列表"><List :size="18"/></button></div></div>
  <div class="shot-toolbar"><div class="shot-filters" aria-label="镜头状态"><button v-for="f in filters" :key="f.id" :aria-pressed="shotFilter===f.id" @click="shotFilter=f.id">{{f.label}} <span>{{f.n}}</span></button></div><label class="shot-search"><Search :size="16"/><input v-model="shotSearch" placeholder="搜索镜头或文件" aria-label="搜索镜头或文件"></label></div>
  <div v-if="!visibleShots.length" class="monitor-empty">没有符合条件的镜头。</div><div class="production-shots" :class="{'compact-shots':compact}"><section v-for="shot in visibleShots" :key="shot.item" class="shot" :data-state="shotState(shot)" :class="{'shot-no-media':!shot.rows.some(j=>j.asset)}"><div class="shot-heading"><h2><span>镜头</span> {{shot.item}}</h2><span>{{shot.done}}/{{shot.rows.length}} · {{time(shot.elapsed_ms)}}</span></div><div class="shot-grid">
    <article v-for="key in shot.keys" :key="key" class="job" v-show="byKey[key]">
      <template v-if="byKey[key]"><div class="job-title"><b>{{stepNames[byKey[key].step]||byKey[key].step}}</b><span class="status" :data-status="byKey[key].status"><i/>{{byKey[key].status_label}}</span></div>
      <button v-if="byKey[key].asset" class="job-media" @click="preview(byKey[key])"><img v-if="thumbs[byKey[key].asset.url]||byKey[key].asset.thumb||byKey[key].asset.kind==='image'" :src="thumbs[byKey[key].asset.url]||byKey[key].asset.thumb||byKey[key].asset.url" @error="fallback($event,byKey[key].asset)" alt="" loading="lazy"><span v-else>查看 {{byKey[key].asset.kind==='video'?'视频':'图片'}}</span></button><div v-else class="job-media waiting"><span class="waiting-frame-number">{{shot.item}}</span><span>{{byKey[key].status_label||'等待媒体'}}</span></div>
      <div class="job-meta"><span>{{(byKey[key].asset?.dimensions||props.mediaIndex?.[byKey[key].asset?.url]?.dimensions)?.join(' × ')||'尺寸未采集'}}</span><span>{{time(byKey[key].elapsed_ms)}}</span></div><p v-if="byKey[key].reason" class="reason">{{byKey[key].reason}}</p>
      <details class="fold small"><summary>阶段耗时、参数与提示词</summary><p>服务执行：{{time(byKey[key].execution_ms)}} · 下载：{{bytes(byKey[key].transferred_bytes)}} · 速度：{{byKey[key].download_bps==null?'未采集':bytes(byKey[key].download_bps)+'/s'}}</p><table><tbody><tr v-for="s in byKey[key].stages||[]" :key="s.stage"><td>{{s.label||s.stage}}</td><td>{{time(s.duration_ms)}}</td><td>{{s.status}}</td></tr></tbody></table><p v-if="byKey[key].prompt" class="prompt">{{byKey[key].prompt}}</p><pre>{{JSON.stringify({parameters:byKey[key].parameters,call_id:byKey[key].call_id,dependencies:byKey[key].dependencies},null,2)}}</pre></details>
      </template>
    </article>
  </div></section></div>
  <details class="fold"><summary>共用模型准备与容器启动</summary><article v-for="model in task.models||[]" :key="model.name" class="model"><h3>{{model.name}} <small>{{model.label}}</small></h3><p>{{model.error||''}} {{time(model.elapsed_ms)}} · {{bytes(model.downloaded_bytes)}} / {{bytes(model.total_bytes)}}</p><table><tbody><tr v-for="(s,i) in model.stages||[]" :key="i"><td>{{s.label||s.stage}}</td><td>{{time(s.duration_ms)}}</td><td>{{s.status}}</td></tr></tbody></table><a v-if="model.log_url" :href="model.log_url" target="_blank">模型日志</a></article><table><tbody><tr v-for="s in task.shared_startup||[]" :key="s.stage"><td>{{s.label||s.stage}}</td><td>{{time(s.duration_ms)}}</td><td>{{s.source}}</td></tr></tbody></table></details>
  <p v-for="n in task.notes||[]" :key="n" class="muted">{{n}}</p>
</section>
</template>
