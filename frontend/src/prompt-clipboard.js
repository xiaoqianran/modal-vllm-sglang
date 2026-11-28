// Copy the exact prompt, including whitespace, with a local-file fallback.
export async function copyPrompt(text, anchor, doc = globalThis.document, clipboard = globalThis.navigator?.clipboard) {
  if (typeof text !== 'string' || !text.trim()) return false
  try {
    if (clipboard?.writeText) { await clipboard.writeText(text); return true }
  } catch { /* Local file and HTTP contexts may require the fallback. */ }
  if (!doc?.execCommand) return false
  const field = doc.createElement('textarea')
  field.value = text
  field.readOnly = true
  field.style.cssText = 'position:fixed;left:0;top:0;opacity:0;pointer-events:none'
  const previous = doc.activeElement
  const parent = anchor?.closest('dialog[open]') || doc.body
  try {
    parent.appendChild(field)
    field.select()
    return doc.execCommand('copy')
  } catch { return false }
  finally { field.remove(); previous?.focus?.({ preventScroll: true }) }
}
