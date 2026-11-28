import test from 'node:test'
import assert from 'node:assert/strict'
import { mergeSnapshot, mergeTaskAssets, monitorAsset, galleryPreview, canFallbackToOriginal } from '../src/catalog.js'

const hf = 'https://huggingface.co/buckets/test/assets/resolve/original.png'
const feed = (asset, extra = {}) => ({name:'001-film',jobs:[{key:'001-image',item:'001',step:'image',prompt:'a frame',asset,...extra}]})

test('verified cloud alias survives stale local feed without duplicate or wrong byte count', () => {
  const a={id:'001-film/001.png',project:'001-film',url:hf,local_url:'001-film/001.png',kind:'image',bytes:50,thumb:hf,artifact:{path:'model-results/a'}}
  const assets=[a]
  mergeTaskAssets(assets,feed({url:a.local_url,kind:'image',bytes:100,thumb:a.local_url}))
  assert.equal(assets.length,1)
  assert.equal(a.url,hf)
  assert.equal(a.bytes,50)
  assert.equal(a.thumb,hf)
  assert.equal(a.artifact.path,'model-results/a')
  assert.equal(a.prompt,'a frame')
})

test('local to cloud transition keeps identity and clears outdated derivative metadata', () => {
  const a={id:'001-film/001-image',project:'001-film',url:'001-film/001.png',kind:'image',thumb:'old',gallery_thumb:'old',preview:'old',digest:'old',artifact:{path:'old'}}
  const assets=[a]
  mergeTaskAssets(assets,feed({url:hf,local_url:a.url,kind:'image',bytes:123}))
  assert.equal(assets.length,1)
  assert.equal(assets[0],a)
  assert.equal(a.storage,'cloud')
  assert.equal(a.thumb,hf)
  assert.equal(a.local_url,'001-film/001.png')
  assert.equal(a.artifact,undefined)
  assert.equal(a.gallery_thumb,undefined)
  assert.equal(a.digest,undefined)
})

test('independent jobs sharing cloud content remain independent assets', () => {
  const assets=[]
  const s=feed({url:hf,kind:'image'})
  s.jobs.push({...s.jobs[0],key:'002-image',item:'002'})
  mergeTaskAssets(assets,s)
  mergeTaskAssets(assets,s)
  assert.equal(assets.length,2)
  assert.notEqual(assets[0].id,assets[1].id)
})

test('video derivative poster survives refresh and never replaces original playback', () => {
  const url=hf.replace('.png','.mp4')
  const assets=[{id:'001-film/001-video',project:'001-film',url,kind:'video',gallery_thumb:'poster.webp',thumb:'poster.webp'}]
  const s=feed({url,kind:'video',thumb:'old-poster.png'},{key:'001-video',step:'video'})
  mergeTaskAssets(assets,s)
  assert.equal(assets[0].thumb,'poster.webp')
  assert.equal(monitorAsset(s.jobs[0],s.name,{[url]:assets[0]}).url,url)
})

test('stale summaries cannot roll back details; fresh summaries preserve jobs', () => {
  const old={updated_at:10,status:'running',jobs:[{key:'a'}]}
  assert.equal(mergeSnapshot(old,{updated_at:9,status:'pending'},true),old)
  assert.deepEqual(mergeSnapshot(old,{updated_at:11,status:'completed',jobs:[]},true),{...old,updated_at:11,status:'completed'})
})

test('monitor carries catalog artifact, dimensions and preview, but not a foreign project artifact', () => {
  const job=feed({url:hf,kind:'image',filename:'file.png'}).jobs[0]
  const item={project:'001-film',url:hf,artifact:{path:'model-results/image'},preview:'preview.webp',dimensions:[2048,1152]}
  const result=monitorAsset(job,'001-film',{['001-film/001-image']:item})
  assert.equal(result.artifact.path,'model-results/image')
  assert.equal(result.preview,'preview.webp')
  assert.deepEqual(result.dimensions,[2048,1152])
  assert.equal(monitorAsset(job,'other',{[hf]:item}).artifact,undefined)
})

test('online checkbox only permits missing remote originals, not video preloading', () => {
  const a={kind:'image',url:hf}
  assert.equal(galleryPreview(a), '')
  assert.equal(galleryPreview(a,true),hf)
  assert.equal(galleryPreview({...a,thumb:'preview.webp'}),'preview.webp')
  assert.equal(galleryPreview({...a,url:'local.png'}),'local.png')
  assert.equal(galleryPreview({...a,kind:'video'},true),'')
  assert.equal(canFallbackToOriginal(a),false)
  assert.equal(canFallbackToOriginal(a,true),true)
})

test('HF native directory owns duplicate gallery URLs without hiding unrelated legacy assets', async () => {
  const { mergeWorkAssets } = await import('../src/catalog.js')
  const url = 'https://huggingface.co/buckets/example/public/resolve/works/029-landscape--hash/001/image.png'
  const native = [{id:'work/image/001',project:'work',url,thumb:'preview.webp'}]
  const old = [
    {id:'029-landscape/image/001',project:'029-landscape',url,thumb:'old.webp'},
    {id:'other/image/001',project:'other',url:'https://huggingface.co/buckets/example/public/resolve/assets/old.png'},
    {id:'work/image/001',project:'work',url:'stale.png'}
  ]
  const merged = mergeWorkAssets(old,native)
  assert.deepEqual(merged,[old[1],native[0]])
  assert.deepEqual(mergeWorkAssets([...old,{id:'local/new',url:'local.png'}],native),
                   [old[1],{id:'local/new',url:'local.png'},native[0]])
  assert.equal(old.length,3)
  assert.equal(native.length,1)
  assert.deepEqual(mergeWorkAssets(old,[]),old)
})
