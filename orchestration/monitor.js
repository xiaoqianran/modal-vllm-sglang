/* File:// compatible refresh: load a local data script, never reload media nodes. */
(() => {
  const initial = JSON.parse(document.getElementById('initial-data').textContent);
  const expectedName = initial.name;
  let latest = initial, loading = false;
  const stepNames = new Map([['first', '首帧'], ['last', '尾帧'], ['video', '视频'], ['image', '图片']]);
  const storyboardSteps = ['first', 'last', 'video'];
  const stageNames = {prepare_service:'CPU 准备服务构建与部署',weights:'权重下载与完整性校验',cache_check:'缓存指纹检查',build_deploy:'推理镜像构建与部署'};
  const text = (node, value) => { if (node) node.textContent = value == null ? '未采集' : String(value); };
  const field = (parent, key, value) => text(parent.querySelector(`[data-field="${key}"]`), value);
  const duration = ms => ms == null ? '未采集' : ms < 1000 ? `${ms.toFixed(0)} 毫秒` : ms < 60000 ? `${(ms/1000).toFixed(1)} 秒` : `${Math.floor(ms/60000)} 分 ${Math.floor(ms/1000)%60} 秒`;
  const bytes = n => n == null ? '未采集' : n >= 1073741824 ? `${(n/1073741824).toFixed(2)} GB` : `${(n/1048576).toFixed(2)} MB`;
  const date = n => n == null ? '未采集' : new Date(n*1000).toLocaleString();
  function badge(node, status, label) { node.className = `badge ${status}`; text(node,label); }
  function stages(parent, values) {
    const body = parent.querySelector('[data-field="stages"]');
    body.replaceChildren(...values.map(s => {
      const row = document.createElement('tr');
      [s.label || stageNames[s.stage] || s.stage, duration(s.duration_ms), s.status === 'unknown' ? '未采集' : s.status === 'running' ? '进行中' : s.status === 'failed' ? '失败尝试' : '已记录', s.source === 'derived' ? '时间戳推导' : s.source === 'unknown' ? '—' : '实测'].forEach(value => {
        const cell = document.createElement('td'); text(cell,value); row.append(cell);
      });
      return row;
    }));
  }
  function createJob(job) {
    const node = document.getElementById('job-template').content.firstElementChild.cloneNode(true);
    node.id = `job-${job.key}`;
    return node;
  }
  async function copyPrompt(value) {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
        return true;
      }
    } catch (_) {
      // Some browsers restrict the Clipboard API on local HTML files.
    }
    const active = document.activeElement;
    const buffer = document.createElement('textarea');
    buffer.className = 'clipboard-buffer';
    buffer.readOnly = true;
    buffer.value = value;
    document.body.append(buffer);
    buffer.select();
    try {
      return document.execCommand('copy');
    } catch (_) {
      return false;
    } finally {
      buffer.remove();
      active?.focus({ preventScroll: true });
    }
  }
  function prepareJob(node, showPrompt) {
    const content = node.querySelector('.job-content');
    const media = document.createElement('div');
    media.className = 'job-media';
    media.append(content.querySelector('[data-field="media"]'));
    const info = document.createElement('div');
    info.className = 'job-info';
    const technical = document.createElement('details');
    technical.className = 'job-details';
    const technicalLabel = document.createElement('summary');
    technicalLabel.textContent = '生成详情';
    technical.append(technicalLabel, ...content.children);
    info.append(technical);
    content.append(media, info);

    const panel = document.createElement('div');
    panel.className = 'prompt-panel';
    const details = document.createElement('details');
    details.open = showPrompt;
    const label = document.createElement('summary');
    label.textContent = '提示词';
    const prompt = document.createElement('div');
    prompt.className = 'prompt-text';
    prompt.dataset.field = 'prompt';
    details.append(label, prompt);

    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'copy-prompt';
    copy.textContent = '复制提示词';
    const feedback = document.createElement('span');
    feedback.className = 'copy-feedback';
    feedback.setAttribute('role', 'status');
    let resetFeedback;
    copy.addEventListener('click', async () => {
      const copied = await copyPrompt(prompt.textContent);
      copy.textContent = copied ? '已复制' : '复制失败';
      feedback.textContent = copied ? '提示词已复制' : '复制失败，请手动选择提示词复制';
      if (!copied) details.open = true;
      clearTimeout(resetFeedback);
      resetFeedback = setTimeout(() => {
        copy.textContent = '复制提示词';
        feedback.textContent = '';
      }, 2500);
    });
    panel.append(details, copy, feedback);
    technical.before(panel);
    info.querySelector('[data-field="parameters"]').previousElementSibling.textContent = '参数与调用记录';
    return prompt;
  }
  function updateMedia(node, asset) {
    const box = node.querySelector('[data-field="media"]');
    if (!asset) { if (!box.querySelector('video,img')) { box.className='preview empty'; text(box,'等待成品'); } return; }
    const url = asset.url;
    let media = box.querySelector(asset.kind === 'video' ? 'video' : 'img');
    if (!media) {
      box.replaceChildren(); box.className='preview';
      if (asset.kind === 'video') {
        media = document.createElement('video'); media.controls=true; media.preload='metadata'; box.append(media);
      } else {
        const anchor = document.createElement('a'); anchor.href=url; anchor.target='_blank';
        media=document.createElement('img'); media.loading='lazy'; media.alt=asset.filename; anchor.append(media); box.append(anchor);
      }
    }
    // Do not assign src again for an unchanged artifact: preserves playback, position and focus.
    if (media.getAttribute('src') !== url) media.setAttribute('src',url);
    const anchor=node.querySelector('[data-field="file"]'); anchor.href=url; text(anchor,asset.filename);
  }
  function updateJob(job, container, showPrompt) {
    let node=document.getElementById(`job-${job.key}`);
    if (!node) { node=createJob(job); container.append(node); }
    const promptNode = node.querySelector('[data-field="prompt"]') || prepareJob(node, showPrompt);
    node.dataset.step = job.step;
    field(node,'step',stepNames.get(job.step) ?? job.step); badge(node.querySelector('[data-field="status"]'),job.status,job.status_label);
    updateMedia(node,job.asset); field(node,'reason',job.reason || '');
    const dims=job.asset?.dimensions;
    if (dims?.[0] > 0 && dims?.[1] > 0) {
      node.style.setProperty('--media-ratio', `${dims[0]} / ${dims[1]}`);
    } else {
      node.style.removeProperty('--media-ratio');
    }
    const request=typeof job.requested === 'string' ? job.requested : JSON.stringify(job.requested);
    field(node,'specs',`请求：${request || '未采集'}\n实际：${dims ? dims.join(' × ') : '未采集'} · 文件：${bytes(job.asset?.bytes)}`);
    const generation=job.stages.find(s=>s.stage==='generate');
    field(node,'generate',duration(generation?.duration_ms)); field(node,'elapsed',duration(job.elapsed_ms));
    field(node,'download',`${bytes(job.transferred_bytes)} · ${job.download_bps == null ? '速度未采集' : bytes(job.download_bps)+'/s'}`);
    field(node,'execution',`服务执行（含交接/获取结果）：${duration(job.execution_ms)}`);
    const prompt = typeof job.prompt === 'string' ? job.prompt : job.prompt == null ? '' : JSON.stringify(job.prompt, null, 2);
    promptNode.closest('.prompt-panel').hidden = !prompt.trim();
    if (promptNode.textContent !== prompt) text(promptNode, prompt);
    field(node,'parameters',JSON.stringify({parameters:job.parameters,call_id:job.call_id,dependencies:job.dependencies},null,2));
    stages(node,job.stages);
  }
  function preparePage() {
    const main = document.querySelector('main');
    const header = main.querySelector('header');
    const shots = document.getElementById('shots');
    const title = document.createElement('div');
    title.className = 'task-title';
    const progress = document.createElement('span');
    progress.id = 'compact-progress';
    title.append(header.querySelector('h1'), document.getElementById('task-status'), progress);
    header.prepend(title);

    const actions = document.createElement('div');
    actions.className = 'task-actions';
    const layout = document.createElement('div');
    layout.id = 'layout-control';
    layout.className = 'layout-control';
    layout.setAttribute('role', 'group');
    layout.setAttribute('aria-label', '镜头布局');
    layout.hidden = true;
    const gallery = document.createElement('button');
    const compare = document.createElement('button');
    for (const [button, view, label] of [[gallery, 'gallery', '画廊'], [compare, 'compare', '并排对照']]) {
      button.type = 'button';
      button.textContent = label;
      button.addEventListener('click', () => setView(view));
      layout.append(button);
    }
    function setView(view) {
      shots.dataset.view = view;
      gallery.setAttribute('aria-pressed', String(view === 'gallery'));
      compare.setAttribute('aria-pressed', String(view === 'compare'));
    }
    setView('gallery');

    const details = document.createElement('details');
    details.className = 'run-details';
    const trigger = document.createElement('summary');
    trigger.textContent = '运行详情';
    const body = document.createElement('div');
    body.className = 'run-details-body';
    body.append(header.querySelector('.toolbar'),
                ...main.querySelectorAll(':scope > .summary-grid, :scope > .section, :scope > .note'));
    details.append(trigger, body);
    actions.append(layout, details);
    header.append(actions);
  }
  function updateModels(data) {
    const host=document.getElementById('models');
    data.models.forEach(model=>{
      let node=document.getElementById(`model-${model.name}`);
      if (!node) { node=document.getElementById('model-template').content.firstElementChild.cloneNode(true); node.id=`model-${model.name}`;host.append(node); }
      field(node,'name',model.name); badge(node.querySelector('[data-field="status"]'),model.status,model.label);
      field(node,'reason',model.error || '');
      field(node,'summary',`共用准备耗时：${duration(model.elapsed_ms)} · 尝试：${model.attempt ?? '未采集'} · 最近更新：${date(model.updated_at)}`);
      field(node,'download',`权重：${bytes(model.downloaded_bytes)} / ${bytes(model.total_bytes)} · 速度：${model.bytes_per_second == null ? '未采集' : bytes(model.bytes_per_second)+'/s'}`);
      const records=[...model.stages];
      if (model.active_stage) records.push({...model.active_stage,status:'running',duration_ms:Math.max(0,(data.updated_at-model.active_stage.started_at)*1000)});
      stages(node,records);
      const link=node.querySelector('[data-field="log"]');link.hidden=!model.log_url;if(model.log_url)link.href=model.log_url;
    });
  }
  function update(data) {
    if (data.name !== expectedName) return;
    latest=data;
    text(document.getElementById('updated'),date(data.updated_at));
    badge(document.getElementById('task-status'),data.status,data.status_label);
    text(document.getElementById('progress'),`${data.counts.completed || 0} / ${data.total}`);
    text(document.getElementById('compact-progress'), `已下载 ${data.downloaded} / ${data.total} · 失败 ${data.counts.failed || 0}`);
    text(document.getElementById('failed'),data.counts.failed || 0);
    text(document.getElementById('downloaded'),`${data.downloaded} / ${data.total}`);
    text(document.getElementById('elapsed'),duration(data.elapsed_ms));
    text(document.getElementById('timing-source'),data.timing_source);
    text(document.getElementById('started'),date(data.started_at));text(document.getElementById('finished'),data.finished_at == null && data.status === 'running' ? '进行中' : date(data.finished_at));
    text(document.getElementById('request-summary'),data.render ? `请求档位：${data.render.resolution} · 比例：${data.render.aspect_ratio}` : '使用已登记工作流尺寸 · 实际尺寸见各成品');
    document.querySelector('#refresh-progress i').style.width=`${data.total ? data.downloaded/data.total*100 : 0}%`;
    const byKey=new Map(data.jobs.map(j=>[j.key,j]));
    let hasStoryboard = false;
    data.shots.forEach(shot=>{
      let node=document.getElementById(`shot-${shot.item}`);
      if(!node){node=document.createElement('section');node.className='shot';node.id=`shot-${shot.item}`;node.innerHTML='<header class="shot-head"><h2></h2><span class="muted" data-shot-summary></span></header><div class="shot-jobs"></div>';text(node.querySelector('h2'),`镜头 ${shot.item}`);document.getElementById('shots').append(node);}
      const group=shot.keys.map(k=>byKey.get(k));
      const storyboard = group.length === storyboardSteps.length && storyboardSteps.every(step => group.some(job => job.step === step));
      node.classList.toggle('storyboard', storyboard);
      hasStoryboard ||= storyboard;
      text(node.querySelector('[data-shot-summary]'), storyboard ? `${group.filter(j => j.asset).length} / ${group.length} 已下载 · 耗时 ${duration(shot.elapsed_ms)}` : '');
      group.forEach(job=>updateJob(job,node.querySelector('.shot-jobs'), group.length === 1));
    });
    document.getElementById('layout-control').hidden = !hasStoryboard;
    stages(document.getElementById('task-stages'),data.stages);
    stages(document.getElementById('shared-stages'),data.shared_startup.filter(s=>s.stage==='startup'));
    updateModels(data);
  }
  preparePage();
  window.updateTaskMonitor=update;
  function refresh() {
    if(loading)return;loading=true;
    const script=document.createElement('script');
    script.src=document.body.dataset.feed+'?v='+Date.now();
    script.onload=()=>{loading=false;script.remove();};
    script.onerror=()=>{loading=false;script.remove();text(document.getElementById('connection'),'读取更新失败，请确认 worker 正在运行且 .runtime 目录仍可访问');};
    document.head.append(script);
  }
  document.getElementById('refresh').addEventListener('click',refresh);
  const auto=document.getElementById('auto-refresh');
  try { auto.checked=localStorage.getItem('monitor-auto')!=='off'; } catch (_) {}
  auto.addEventListener('change',()=>{try{localStorage.setItem('monitor-auto',auto.checked?'on':'off');}catch(_){} });
  setInterval(()=>{if(auto.checked)refresh();},5000);
  setInterval(()=>{
    const age=Math.max(0,Date.now()/1000-latest.updated_at), node=document.getElementById('connection');
    if(latest.monitor_static){node.classList.remove('stale');text(node,'已保存的历史记录');return;}
    node.classList.toggle('stale',age>20);
    text(node,age>20?`数据已 ${Math.floor(age)} 秒未更新，请检查 worker`:`${auto.checked?'自动更新已开启':'自动更新已关闭'} · 视频播放不会被刷新打断`);
  },1000);
  update(initial);
})();
