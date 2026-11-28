<script setup>
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { validateTask, submitTask, getTask, resumeTask, getModels } from './api.js'
import { availableModels, canvasDimensions, validCanvas, validSeed, draftTask } from './atelier-task.js'
import { Aperture, ImagePlus, Download, Film, X, Search, Sparkles, Maximize2, ChevronRight } from 'lucide-vue-next'
const props=defineProps({assets:{type:Array,default:()=>[]},source:Object,live:Boolean})
const emit=defineEmits(['browse','preview'])
const model=ref('minimax'),mode=ref('t2i'),prompt=ref(''),ratio=ref('16:9'),quality=ref('2k'),name=ref('099-atelier'),count=ref(1),search=ref(''),chosen=ref(null),notice=ref('')
const path=ref(''),digest=ref(''),size=ref(''),seed=ref(42),submitting=ref(false),taskId=ref(''),safeToClose=ref(false),taskStatus=ref('')
const lastTaskKey = 'studio.lastTask'
const capabilities=ref(null),connected=ref(false),capabilityError=ref(''),delivery=ref('project')
const models=computed(()=>availableModels(capabilities.value))
const activeModel=computed(()=>models.value.find(m=>m.id===model.value))
const workflow=computed(()=>activeModel.value?.workflows[mode.value]||'')
const useSeed=computed(()=>model.value.startsWith('minimax'))
const canSend=computed(()=>props.live&&connected.value)
const statusLabels = {
  local_registered: '本地已登记',
  submitting: '云端提交中',
  submission_uncertain: '提交结果不确定',
  cloud_owned: '云端已接管',
  delivering: '正在交付',
  completed: '已完成',
  failed: '任务失败',
}
let pollTimer
let polling = false
let mounted = true
const images=computed(()=>props.assets.filter(a=>a.kind==='image'))
const library=computed(()=>images.value.filter(a=>[a.title,a.name,a.project].join(' ').toLowerCase().includes(search.value.toLowerCase())).slice(0,30))
const dims=computed(()=>canvasDimensions(ratio.value,quality.value))
const validSize=computed(()=>validCanvas(model.value,dims.value))
const original=computed(()=>/^model-results\/[A-Za-z0-9_./-]+$/.test(path.value)&&!path.value.split('/').includes('..')&&/^[a-f0-9]{64}$/.test(digest.value)&&Number.isSafeInteger(Number(size.value))&&Number(size.value)>0&&Number(size.value)<=67108864)
const ready=computed(()=>prompt.value.trim()&&/^\d{3}(?:-[a-z0-9]+)+$/.test(name.value)&&!!workflow.value&&validSize.value&&(!useSeed.value||validSeed(seed.value))&&(mode.value!=='i2i'||original.value)&&Number.isInteger(count.value)&&count.value>=1&&count.value<=12)
function choose(a){chosen.value=a;const editor=activeModel.value?.workflows.i2i?activeModel.value:models.value.find(m=>m.workflows.i2i);if(editor){model.value=editor.id;mode.value='i2i'}else{mode.value='t2i'};path.value=a.artifact?.path||'';digest.value=a.artifact?.sha256||'';size.value=a.artifact?.size||'';notice.value=''}
watch(()=>props.source,a=>{if(a)choose(a)},{immediate:true})
watch([model,models],()=>{if(!activeModel.value)model.value=models.value[0]?.id||'';if(!activeModel.value?.workflows[mode.value])mode.value=activeModel.value?.workflows.t2i?'t2i':'i2i'})
watch([mode,model,prompt,ratio,quality,name,count,path,digest,size,seed,delivery],()=>notice.value='')
const task=computed(()=>draftTask({name:name.value,workflow:workflow.value,ratio:ratio.value,quality:quality.value,prompt:prompt.value,count:count.value,seed:useSeed.value?seed.value:undefined,delivery:delivery.value,image:mode.value==='i2i'?{path:path.value,sha256:digest.value,size:Number(size.value)}:undefined}))
async function connect(){
 if(!props.live)return
 connected.value=false;capabilityError.value=''
 try{
  const data=await getModels()
  if(!mounted)return
  if(!data.capabilities||typeof data.capabilities!=='object'||Array.isArray(data.capabilities))throw Error('模型能力数据无效')
  capabilities.value=data.capabilities;connected.value=true
 }catch(e){if(mounted)capabilityError.value='本地 Studio 能力读取失败：'+e.message}
}
function save(){if(!ready.value)return;const url=URL.createObjectURL(new Blob([JSON.stringify(task.value,null,2)+'\n'],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name.value+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notice.value='任务草稿已导出。尚未提交云端。'}
async function validate(){if(!ready.value||!canSend.value||submitting.value)return;submitting.value=true;try{const result=await validateTask(task.value);notice.value='本地校验通过：'+result.jobs+' 个任务 · 交付：'+(result.delivery?.provider||'local')+' · 不会启动 GPU。'}catch(e){notice.value='校验失败：'+e.message}finally{submitting.value=false}}
const recoveryRequired = ref(false)
function showStatus(result) {
  recoveryRequired.value = result.recovery_required
  taskStatus.value = result.status
  safeToClose.value = result.safe_to_close
  if (result.recovery_required) notice.value = result.worker_error || '原云端控制器已停止，可以恢复云端执行。'
  else if (result.archive_errors) notice.value = (result.studio_safe_to_close ? '可以关闭 Studio。' : '请保持 Studio 运行。') + (result.archive_blocked ? 'HF 归档已受阻，请检查权限或完整性错误。' : 'HF 交付出现错误，请查看归档记录。')
  else if (result.worker_error) notice.value = '本地调度需要检查：' + result.worker_error
  else if (result.status === 'submission_uncertain') notice.value = '正在核对原有云端调用。请保持 Studio 运行。'
  else if (result.status === 'failed') notice.value = '任务存在失败项，请在监控中查看结果。'
  else if (result.status === 'completed') notice.value = '创作与交付已完成。'
  else if (result.studio_safe_to_close) notice.value = '剩余工作可在云端继续，可以关闭本地 Studio。'
  else if (result.safe_to_close) notice.value = '本任务可在云端继续，Studio 仍有其他任务需要本地调度。'
  else notice.value = '仍有工作需要本地 Studio，请保持运行。'
}

async function submit() {
  if (!ready.value || !canSend.value || submitting.value) return
  submitting.value = true
  const definition = task.value
  let registering = false
  try {
    const checked = await validateTask(definition)
    clearTimeout(pollTimer)
    notice.value = '本地校验通过，正在登记 ' + checked.jobs + ' 个任务…'
    registering = true
    const result = await submitTask(definition)
    rememberTask(result.task_id)
    showStatus(result)
  } catch (error) {
    if (registering && !error.status) {
      rememberTask(definition.name)
      safeToClose.value = false
      taskStatus.value = 'submission_uncertain'
    }
    notice.value = registering ? '未收到登记确认：' + error.message : '本地校验失败：' + error.message
  } finally {
    submitting.value = false
    if (taskId.value) pollStatus()
  }
}

function rememberTask(id) {
  taskId.value = id
  try { localStorage.setItem(lastTaskKey, id) } catch {}
}

async function pollStatus() {
  const id = taskId.value
  if (!props.live || !id || polling || !mounted) return
  clearTimeout(pollTimer)
  polling = true
  let again = true
  try {
    const result = await getTask(id)
    if (id !== taskId.value || !mounted) return
    showStatus(result)
    again = !['completed', 'failed'].includes(result.status)
  } catch (error) {
    if (id === taskId.value && mounted) {
      safeToClose.value = false
      again = error.status !== 404
      notice.value = again ? '状态查询暂时失败：' + error.message : '本地任务目录暂无这项记录。可用相同任务 JSON 重试登记。'
    }
  } finally {
    polling = false
    if (mounted && (again || id !== taskId.value)) pollTimer = setTimeout(pollStatus, 2000)
  }
}

async function resume() {
  if (!canSend.value || !taskId.value || submitting.value) return
  submitting.value = true
  try {
    showStatus(await resumeTask(taskId.value, recoveryRequired.value))
    pollStatus()
  } catch (error) {
    notice.value = '恢复失败：' + error.message
  } finally {
    submitting.value = false
  }
}
function imageError(e,a){if(a.url&&e.currentTarget.getAttribute('src')!==a.url)e.currentTarget.src=a.url}
onMounted(() => {
  if(!props.live)return
  connect()
  try { taskId.value = localStorage.getItem(lastTaskKey) || '' } catch {}
  pollStatus()
})
onUnmounted(() => { mounted = false; clearTimeout(pollTimer) })
</script>
<template>
<main class="atelier">
 <header class="atelier-heading"><div><p>Digital Film Archive / 创作</p><h1>Atelier<span>光影工作室</span></h1></div><button class="atelier-quiet" @click="emit('browse')"><Film :size="17"/> 返回作品库 <ChevronRight :size="15"/></button></header>
 <div class="atelier-layout">
  <section class="atelier-workspace" aria-label="创作画布">
   <div class="atelier-stage-bar"><span><Aperture :size="17"/> {{chosen?'参考画面':'新画布'}}</span><span>{{dims.join(' × ')}} <button v-if="chosen" class="atelier-quiet" aria-label="移除参考图" @click="chosen=null;path='';digest='';size='';mode='t2i'"><X :size="16"/></button></span></div>
   <div class="atelier-stage"><button v-if="chosen" class="atelier-frame" @click="emit('preview',chosen)"><img :src="chosen.preview||chosen.thumb||chosen.url" :alt="chosen.title||chosen.name" @error="imageError($event,chosen)"><span><Maximize2 :size="15"/> 查看作品</span></button><div v-else class="atelier-empty"><Aperture :size="52" :stroke-width="1"/><h2>从一束光开始。</h2><p>写下你的构想，或从作品库选择一张画面。</p><button class="atelier-quiet" @click="mode='t2i'"><Sparkles :size="16"/> 新建画面</button></div></div>
   <div class="atelier-stage-caption"><span>{{chosen?.title||chosen?.name||'未命名画面'}}</span><small>{{chosen?.project||'等待创作指令'}}</small></div>
   <div class="atelier-library-head"><h2>参考片库 <small>{{images.length}} 张</small></h2><label><Search :size="15"/><input v-model="search" placeholder="查找参考图" aria-label="查找参考图"></label></div>
   <div class="atelier-contact"><button v-for="a in library" :key="a.id" :class="{selected:chosen?.id===a.id}" :aria-pressed="chosen?.id===a.id" @click="choose(a)"><img :src="a.thumb||a.url" :alt="a.title||a.name" loading="lazy" @error="imageError($event,a)"><small>{{a.title||a.name}}</small></button><p v-if="!library.length">暂无可显示的作品。打开本地馆藏以载入已有图片。</p></div>
  </section>
  <aside class="atelier-inspector" aria-label="创作设置">
   <h2>创作指令</h2><div class="atelier-modes"><button :aria-pressed="mode==='t2i'" :disabled="!activeModel?.workflows.t2i" @click="mode='t2i'">文生图</button><button :aria-pressed="mode==='i2i'" :disabled="!activeModel?.workflows.i2i" @click="mode='i2i'">图生图</button></div>
   <label>模型<select v-model="model"><option v-for="m in models" :key="m.id" :value="m.id">{{m.label}}</option></select></label>
   <label>提示词<textarea v-model="prompt" rows="6" :placeholder="mode==='i2i'?'描述希望修改的内容，保留原图的哪些特征…':'描述构图、光线、色彩与故事…'"></textarea></label>
   <details v-if="mode==='i2i'" class="atelier-original" :open="!original"><summary>编辑原图 {{original?'已填写引用':'需要原图引用'}}</summary><p>预览图不会上传。填写正式资产的 Volume 路径、SHA-256 与原始字节数。</p><label>Volume 路径<input v-model.trim="path" placeholder="model-results/…"></label><label>SHA-256<input v-model.trim="digest" placeholder="64 位十六进制"></label><label>字节数<input v-model="size" type="number" min="1" max="67108864"></label></details>
   <label v-if="useSeed">起始随机种子（批次逐张递增）<input v-model.number="seed" type="number" min="0" max="4294967295"></label>
   <fieldset><legend>画幅</legend><div class="atelier-ratios"><button v-for="r in ['16:9','4:3','1:1','3:4','9:16']" :key="r" :aria-pressed="ratio===r" @click="ratio=r">{{r}}</button></div></fieldset>
   <fieldset><legend>分辨率 <small>{{dims.join(' × ')}}</small></legend><div class="atelier-modes"><button v-for="q in ['1k','2k','4k']" :key="q" :aria-pressed="quality===q" @click="quality=q">{{q.toUpperCase()}}</button></div><p v-if="!validSize" class="atelier-warning">当前画幅不符合 H3 的 32 像素对齐或最低像素要求。</p></fieldset>
   <div class="atelier-fields"><label>任务名称<input v-model.trim="name" placeholder="099-atelier"></label><label>数量<input v-model.number="count" type="number" min="1" max="12"></label></div>
   <label>交付方式<select v-model="delivery"><option value="project">项目配置（默认）</option><option value="huggingface">HF 默认归档桶</option><option value="local">仅本地</option></select></label>
   <p v-if="!live" class="atelier-note">离线馆藏 · 仅可导出任务 JSON。运行 python run.py studio 后可提交。</p><p v-else-if="!connected" class="atelier-note" role="status">{{capabilityError||'正在读取模型能力…'}} <button v-if="capabilityError" class="atelier-quiet" @click="connect">重试</button></p>
   <div class="atelier-actions"><button class="atelier-validate" :disabled="!ready||!canSend||submitting" @click="validate">本地校验</button><button class="atelier-submit" :disabled="!ready||!canSend||submitting" @click="submit"><Sparkles :size="18"/> {{submitting?'正在处理…':'提交正式任务'}}</button><button class="atelier-quiet" :disabled="!ready" @click="save"><Download :size="16"/> 导出 JSON</button></div>
   <p v-if="taskId" class="atelier-task-state"><strong>{{taskId}}</strong><span :class="{owned:safeToClose}">{{statusLabels[taskStatus] || '正在读取状态'}}</span></p>
   <button v-if="live && taskId && taskStatus && !['completed', 'failed'].includes(taskStatus)" class="atelier-validate" :disabled="!canSend||submitting" @click="resume">{{ recoveryRequired ? '恢复云端执行' : '同步任务' }}</button>
   <p class="atelier-note" role="status">{{notice||'本地设计预览。导出正式 Task JSON，不会启动 GPU 或产生费用。'}}</p>
   <details class="atelier-json"><summary>查看任务 JSON</summary><pre>{{JSON.stringify(task,null,2)}}</pre></details>
  </aside>
 </div>
</main>
</template>
