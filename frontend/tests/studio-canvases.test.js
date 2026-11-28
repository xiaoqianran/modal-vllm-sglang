import test from 'node:test'
import assert from 'node:assert/strict'
import { modelCanvases, canvasOptions, selectCanvas } from '../src/atelier-task.js'
test('live catalog uses backend canvas values, including nonstandard dimensions',()=>{
 const m={id:'new-engine',workflows:{t2i:'custom'},canvases:{t2i:{'2k':{'16:9':[2304,1296]},'4k':{'1:1':[4096,4096]}}}}
 const c=modelCanvases(m,'t2i')
 assert.deepEqual(c['2k']['16:9'],[2304,1296])
 assert.deepEqual(canvasOptions(c),['16:9','1:1'])
 assert.deepEqual(selectCanvas(c,'1:1','2k'),{ratio:'1:1',quality:'4k'})
 assert.deepEqual(selectCanvas(c,'16:9','2k'),{ratio:'16:9',quality:'2k'})
 assert.deepEqual(modelCanvases(m,'i2i'),{})
})
test('offline fallback does not advertise invalid H3 canvas',()=>{
 const c=modelCanvases({id:'minimax',workflows:{t2i:'minimax-t2i'}},'t2i')
 assert.equal(c['1k']['16:9'],undefined)
 assert.deepEqual(selectCanvas(c,'16:9','1k'),{ratio:'16:9',quality:'2k'})
})
