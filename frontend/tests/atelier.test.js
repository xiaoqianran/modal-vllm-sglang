import test from 'node:test'
import assert from 'node:assert/strict'
import { availableModels, canvasDimensions, validCanvas, validSeed, draftTask } from '../src/atelier-task.js'
import { mergeCatalogTasks, mergeLiveAssets } from '../src/catalog.js'

test('live model list reflects capability output and individual modes',()=>{
 const models=availableModels({'moss.t2i':{output:'image'},'minimax-omni.i2i':{output:'image'},'minimax.t2i':{output:'video'}})
 assert.deepEqual(models.map(m=>m.id),['minimax-omni','moss'])
 assert.deepEqual(models[0].workflows,{i2i:'minimax-omni-i2i'})
 assert.deepEqual(availableModels({}),[])
})
test('model canvas and seed validation',()=>{
 assert.deepEqual(canvasDimensions('16:9','2k'),[2048,1152])
 assert.equal(validCanvas('minimax-omni',canvasDimensions('16:9','1k')),false)
 assert.equal(validCanvas('minimax-omni',canvasDimensions('16:9','2k')),true)
 assert.equal(validCanvas('moss',canvasDimensions('16:9','1k')),true)
 assert.equal(validSeed(4294967295),true)
 assert.equal(validSeed(4294967296),false)
 assert.equal(validSeed('42'),false)
})
test('draft uses project delivery config and distinct reproducible seeds',()=>{
 const args={name:'099-test',workflow:'minimax-omni-i2i',ratio:'16:9',quality:'2k',prompt:' edit ',count:3,seed:4294967295,image:{path:'model-results/a',sha256:'a'.repeat(64),size:123},delivery:'project'}
 const task=draftTask(args)
 assert.equal('delivery' in task,false)
 assert.deepEqual(task.items.map(i=>i.seed),[4294967295,0,1])
 assert.equal(task.items[0].prompt,'edit')
 assert.deepEqual(task.items[0].image.artifact,args.image)
 assert.deepEqual(draftTask({...args,delivery:'huggingface'}).delivery,{provider:'huggingface'})
 assert.deepEqual(draftTask({...args,delivery:'local'}).delivery,{provider:'local'})
})
test('cached live catalog cannot roll back selected monitor snapshot',()=>{
 const tasks=[{id:'x',feed:'old.js',snapshot:{updated_at:12,status:'completed',jobs:[{key:'a'}]}}]
 mergeCatalogTasks(tasks,[{id:'x',feed:'new.js',snapshot:{updated_at:11,status:'running'}},{id:'y',snapshot:{status:'pending'}}])
 assert.equal(tasks[0].snapshot.status,'completed')
 assert.equal(tasks[0].snapshot.jobs.length,1)
 assert.equal(tasks[0].feed,'new.js')
 assert.equal(tasks.length,2)
})
test('live catalog canonicalizes local alias without losing feed-only assets',()=>{
 const previous=[{id:'job',url:'a.png'},{id:'new',url:'b.png'}]
 const incoming=[{id:'canonical',url:'https://example.test/a.png',local_url:'a.png'}]
 assert.deepEqual(mergeLiveAssets(previous,incoming),[incoming[0],previous[1]])
})
