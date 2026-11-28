import { test } from 'node:test'
import assert from 'node:assert/strict'
import { copyPrompt } from '../src/prompt-clipboard.js'

test('copies original prompt without trimming or changing newlines', async () => {
  const original = '  光线与构图\n\ncinematic scene  '
  let copied
  assert.equal(await copyPrompt(original, null, {}, { writeText: async value => { copied = value } }), true)
  assert.equal(copied, original)
})

test('falls back inside modal and restores focus after clipboard rejection', async () => {
  const calls = []
  const field = { style: {}, select: () => calls.push('select'), remove: () => calls.push('remove') }
  const dialog = { appendChild: value => { assert.equal(value, field); calls.push('append') } }
  const doc = { createElement: () => field, activeElement: { focus: () => calls.push('focus') }, execCommand: command => { assert.equal(command, 'copy'); calls.push('copy'); return true } }
  const anchor = { closest: selector => { assert.equal(selector, 'dialog[open]'); return dialog } }
  assert.equal(await copyPrompt('prompt', anchor, doc, { writeText: async () => { throw Error('denied') } }), true)
  assert.equal(field.value, 'prompt')
  assert.deepEqual(calls, ['append', 'select', 'copy', 'remove', 'focus'])
})

test('empty prompts and unsupported clipboard report failure', async () => {
  assert.equal(await copyPrompt('  ', null, {}, {}), false)
  assert.equal(await copyPrompt('prompt', null, {}, {}), false)
})
