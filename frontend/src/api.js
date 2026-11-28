let sessionToken

async function jsonRequest(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    cache: 'no-store',
    ...options,
    headers: { Accept: 'application/json', ...(options.headers || {}) }
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(body.error || ('Local Studio request failed (' + response.status + ')'))
    error.status = response.status
    throw error
  }
  return body
}

async function token() {
  if (!sessionToken) {
    const session = await jsonRequest('/api/session')
    sessionToken = session.token
  }
  return sessionToken
}

async function post(path, body) {
  const send = async () => jsonRequest(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': await token() },
    body: JSON.stringify(body)
  })
  try {
    return await send()
  } catch (error) {
    if (error.status !== 403) throw error
    sessionToken = undefined
    return send()
  }
}

export const validateTask = task => post('/api/tasks/validate', task)
export const submitTask = task => post('/api/tasks', task)
export const resumeTask = (id, recover) => post('/api/tasks/' + encodeURIComponent(id) + '/resume', recover ? { recover: true } : {})
export const getTask = id => jsonRequest('/api/tasks/' + encodeURIComponent(id))
export const getCatalog = () => jsonRequest('/api/catalog')

export const getModels = () => jsonRequest('/api/models')
