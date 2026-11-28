// Only expose image workflows with a formal Task contract.
export const imageModels = [
  { id:'minimax', label:'MiniMax H3 Image · SGLang', workflows:{t2i:'minimax-t2i',i2i:'minimax-i2i'} },
  { id:'minimax-omni', label:'MiniMax H3 Image · vLLM Omni', workflows:{t2i:'minimax-omni-t2i',i2i:'minimax-omni-i2i'} },
  { id:'moss', label:'MOSS Image', workflows:{t2i:'moss-t2i'} },
]
export function availableModels(capabilities=null) {
  if(capabilities===null)return imageModels
  return imageModels.flatMap(m=>{
    const workflows=Object.fromEntries(Object.entries(m.workflows).filter(([mode])=>capabilities[m.id+'.'+mode]?.output==='image'))
    return Object.keys(workflows).length?[{...m,workflows}]:[]
  })
}
export function canvasDimensions(ratio,quality){
  const edge={'1k':1024,'2k':2048,'4k':4096}[quality],[w,h]=ratio.split(':').map(Number)
  return [Math.floor(edge*w/Math.max(w,h)),Math.floor(edge*h/Math.max(w,h))]
}
export function validCanvas(model,dims){
  return dims.every(v=>Number.isInteger(v)&&v>0)&&(!model.startsWith('minimax')||(dims.every(v=>v%32===0)&&dims[0]*dims[1]>=1048576&&dims[0]*dims[1]<=20971520))
}
export const validSeed=seed=>Number.isInteger(seed)&&seed>=0&&seed<2**32
export function draftTask({name,workflow,ratio,quality,prompt,count,seed,image,delivery}){
  return {version:1,name,workflow,render:{aspect_ratio:ratio,resolution:quality},
    ...(delivery==='project'?{}:{delivery:{provider:delivery}}),
    items:Array.from({length:Math.max(0,Math.min(12,Number(count)||0))},(_,i)=>({
      id:String(i+1).padStart(3,'0'),prompt:prompt.trim(),
      ...(seed===undefined?{}:{seed:(seed+i)%2**32}),
      ...(image?{image:{artifact:image}}:{})
    }))}
}

export const offlineRatios = ['16:9','4:3','1:1','3:4','9:16']
export const offlineQualities = ['1k','2k','4k']

// In live mode the model validator, not a browser formula, decides acceptance.
export function modelCanvases(model, mode) {
  if (!model?.workflows[mode]) return {}
  if (model.canvases) return model.canvases[mode] || {}
  return Object.fromEntries(offlineQualities.map(q => [q, Object.fromEntries(
    offlineRatios.map(r => [r, canvasDimensions(r,q)]).filter(([,d]) => validCanvas(model.id,d))
  )]))
}
export function canvasOptions(canvases) {
  return [...new Set(Object.values(canvases).flatMap(c => Object.keys(c)))]
}
export function selectCanvas(canvases, ratio, quality) {
  if (canvases[quality]?.[ratio]) return {ratio,quality}
  const tiers=Object.keys(canvases)
  const chosen=tiers.find(q=>canvases[q][ratio]&&q==='2k')||tiers.find(q=>canvases[q][ratio])
  if(chosen)return {ratio,quality:chosen}
  const q=canvases[quality]?quality:tiers[0]
  return {ratio:Object.keys(canvases[q]||{})[0]||ratio,quality:q||quality}
}
