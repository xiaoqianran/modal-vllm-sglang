/* File:// compatible refresh: load a local data script, never reload media nodes. */
(() => {
  const initial = JSON.parse(document.getElementById('initial-data').textContent);
  const expectedName = initial.name;
  let latest = initial, loading = false;
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
  function updateJob(job, container) {
    let node=document.getElementById(`job-${job.key}`);
    if (!node) { node=createJob(job); container.append(node); }
    field(node,'step',job.step); badge(node.querySelector('[data-field="status"]'),job.status,job.status_label);
    updateMedia(node,job.asset); field(node,'reason',job.reason || '');
    const dims=job.asset?.dimensions;
    const request=typeof job.requested === 'string' ? job.requested : JSON.stringify(job.requested);
    field(node,'specs',`请求：${request || '未采集'}\n实际：${dims ? dims.join(' × ') : '未采集'} · 文件：${bytes(job.asset?.bytes)}`);
    const generation=job.stages.find(s=>s.stage==='generate');
    field(node,'generate',duration(generation?.duration_ms)); field(node,'elapsed',duration(job.elapsed_ms));
    field(node,'download',`${bytes(job.transferred_bytes)} · ${job.download_bps == null ? '速度未采集' : bytes(job.download_bps)+'/s'}`);
    field(node,'execution',`服务执行（含交接/获取结果）：${duration(job.execution_ms)}`);
    field(node,'parameters',JSON.stringify({prompt:job.prompt,parameters:job.parameters,call_id:job.call_id,dependencies:job.dependencies},null,2));
    stages(node,job.stages);
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
    text(document.getElementById('failed'),data.counts.failed || 0);
    text(document.getElementById('downloaded'),`${data.downloaded} / ${data.total}`);
    text(document.getElementById('elapsed'),duration(data.elapsed_ms));
    text(document.getElementById('timing-source'),data.timing_source);
    text(document.getElementById('started'),date(data.started_at));text(document.getElementById('finished'),data.finished_at == null && data.status === 'running' ? '进行中' : date(data.finished_at));
    text(document.getElementById('request-summary'),data.render ? `请求档位：${data.render.resolution} · 比例：${data.render.aspect_ratio}` : '使用已登记工作流尺寸 · 实际尺寸见各成品');
    document.querySelector('#refresh-progress i').style.width=`${data.total ? data.downloaded/data.total*100 : 0}%`;
    const byKey=new Map(data.jobs.map(j=>[j.key,j]));
    data.shots.forEach(shot=>{
      let node=document.getElementById(`shot-${shot.item}`);
      if(!node){node=document.createElement('section');node.className='shot';node.id=`shot-${shot.item}`;node.innerHTML='<header class="shot-head"><h2></h2><span class="muted" data-shot-summary></span></header><div class="shot-jobs"></div>';text(node.querySelector('h2'),`镜头 ${shot.item}`);document.getElementById('shots').append(node);}
      const group=shot.keys.map(k=>byKey.get(k));
      text(node.querySelector('[data-shot-summary]'),`${group.filter(j=>j.status==='completed').length}/${group.length} 步骤完成 · 镜头已观测耗时：${duration(shot.elapsed_ms)}`);
      group.forEach(job=>updateJob(job,node.querySelector('.shot-jobs')));
    });
    stages(document.getElementById('task-stages'),data.stages);
    stages(document.getElementById('shared-stages'),data.shared_startup.filter(s=>s.stage==='startup'));
    updateModels(data);
  }
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
    node.classList.toggle('stale',age>20);
    text(node,age>20?`数据已 ${Math.floor(age)} 秒未更新，请检查 worker`:`${auto.checked?'自动更新已开启':'自动更新已关闭'} · 视频播放不会被刷新打断`);
  },1000);
  update(initial);
})();
