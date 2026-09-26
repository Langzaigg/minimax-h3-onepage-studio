/* Named, owner-scoped libraries. Files remain server assets when saving a draft. */
(() => {
  'use strict';
  const base = '/api/studio/workspaces';
  const panel = $('workspace-panel');
  const fields = ['image-prompt','image-size','image-seed','image-auto-polish','image-purpose','prompt','video-auto-postprocess','duration','megapixels','aspect','target-resolution','source-resolution-profile','seed','polish-before-generate','hybrid-preset','hybrid-turbo','nsfw','ref-image-size','use-video-audio','finish-prompt','finish-method','finish-fps','finish-cadence','finish-loop','finish-duration','finish-crossfade','finish-seed','finish-title-fade','finish-interpolation','finish-dlss-controls-version','finish-dlss-style','finish-dlss-experimental','finish-dlss-output-blend','finish-dlss-output-blend-enabled','finish-dlss-local-tone','finish-dlss-local-tone-enabled','finish-dlss-local-structure','finish-dlss-local-structure-enabled','finish-dlss-skin-mask','finish-dlss-skin-mask-enabled','finish-dlss-look','finish-dlss-strength','finish-preview-frame','finish-preview-time','finish-preview-mode','finish-overlay-enabled','finish-overlay-anchor','finish-overlay-width-percent','finish-overlay-opacity','finish-overlay-start','finish-overlay-end','finish-overlay-loop','finish-overlay-size-mode','finish-overlay-offset-x','finish-overlay-offset-y','finish-overlay-x','finish-overlay-y','finish-overlay-width','finish-overlay-height','finish-overlay-frame-time'];
  let current = null, libraries = [], selectedAsset = null, pendingTask = null, requestEpoch = 0;
  const selectedIds=new Set(), mime='application/x-frame-studio-asset';
  let trayHeight=188, collapsed=true, draggingPayload=null, hoverTimer=null;
  let fileOrigins = new WeakMap(), nameAction = 'new', confirmAction = null;
  const json = data => ({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const fail = error => { if (error.name !== 'ReuseCancelled') toast(error.message, true); };
  const operation = (title, fn, focus = false) => {requestEpoch++;return runStudioOperation(title, fn, focus).catch(fail)};
  const esc = escapeHtml;
  const dateLabel = value => value ? new Date(value).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
  const mediaLabel = asset => [({image:'图片',video:'视频',audio:'音频'})[asset.kind] || '素材', asset.width&&asset.height?`${asset.width} × ${asset.height}`:'', asset.duration?`${Number(asset.duration).toFixed(1)} 秒`:''].filter(Boolean).join(' · ');

  function close() {
    collapsed=true;applyTraySize();
  }
  async function open() {
    setHistoryOpen(false);
    collapsed=false;applyTraySize();
    await refreshList();
  }
  function applyTraySize(){panel.classList.toggle('tray-collapsed',collapsed);panel.classList.remove('tray-grid');document.body.classList.toggle('materials-open',!collapsed);document.documentElement.style.setProperty('--library-width',collapsed?'44px':'300px');document.documentElement.style.setProperty('--tray-height','0px');panel.style.width=collapsed?'44px':'300px';for(const id of ['ws-select','ws-asset-count','ws-search','ws-upload-button','ws-new','ws-load','ws-save','ws-more'])$(id).hidden=collapsed;for(const node of panel.querySelectorAll('.tray-body,.ws-asset-selection,.ws-pending'))node.hidden=collapsed;$('ws-close').innerHTML='<span>素材</span>';$('ws-close').setAttribute('aria-expanded',String(!collapsed));$('ws-close').title=collapsed?'展开素材侧栏':'收起素材侧栏';$('workspace-toggle').setAttribute('aria-expanded',String(!collapsed));}
  function renderList() {
    $('ws-select').replaceChildren(new Option('选择或新建工作区',''), ...libraries.map(item=>new Option(`${item.name} · ${item.asset_count||0} 个素材`,item.id)));
    $('ws-select').value = current?.id || '';
  }
  async function refreshList() {
    const epoch = ++requestEpoch;
    try {
      const response = await api(base);
      if (epoch !== requestEpoch) return;
      libraries = response.workspaces || [];
      if (current && !libraries.some(item=>item.id===current.id)) current = null;
      renderList(); render();
      if(!current&&libraries.length){const preferred=localStorage.getItem('frame_studio_workspace');await select(libraries.find(item=>item.id===preferred)?.id||libraries[0].id)}
    } catch(error) { if(epoch===requestEpoch) { $('ws-status').textContent='读取失败，请重试'; fail(error); } }
  }
  function accept(detail) {
    current = detail;
    localStorage.setItem('frame_studio_workspace',detail.id);
    for(const id of selectedIds)if(!detail.assets.some(asset=>asset.id===id))selectedIds.delete(id);
    const summary = {...detail,asset_count:detail.assets?.length||0};
    libraries = [summary,...libraries.filter(item=>item.id!==detail.id)];
    renderList(); render();
    if ($('ws-history').open) loadVersions();scheduleSessionSave();
  }
  async function select(id) {
    const epoch = ++requestEpoch;
    if (!id) { current = null; selectedAsset = null; selectedIds.clear(); render(); return; }
    $('ws-status').textContent='正在读取素材库…';
    try { const detail=await api(`${base}/${id}`); if(epoch===requestEpoch) { selectedAsset=null; selectedIds.clear(); accept(detail); } }
    catch(error) { if(epoch===requestEpoch) { renderList(); render(); fail(error); } }
  }
  function render() {
    const has = !!current;
    $('ws-empty').classList.toggle('hidden',has); $('ws-content').classList.toggle('hidden',!has);
    $('ws-rename').disabled = !has; $('ws-save').disabled = !has; $('ws-delete').disabled=!has;$('ws-upload-button').disabled=!has;
    $('ws-load').disabled = !has || !current.draft || !Object.keys(current.draft).length;
    $('ws-task-add').disabled = !has;
    $('ws-status').textContent = has ? `${current.name} · 版本 ${current.revision}` : '素材、参数与版本';
    if (!has) { $('ws-assets').replaceChildren(); $('ws-asset-count').textContent='0 个';$('ws-asset-selection').classList.add('hidden');$('ws-export').removeAttribute('href');return; }
    $('ws-export').href = `${base}/${current.id}/export`;
    $('ws-asset-count').textContent = `${current.assets.length} 个`;
    const query = $('ws-search').value.trim().toLowerCase();
    const assets = current.assets.filter(asset=>!query||`${asset.name} ${mediaLabel(asset)}`.toLowerCase().includes(query));
    $('ws-assets').innerHTML = assets.map(asset=>`<article class="ws-asset${selectedIds.has(asset.id)?' selected':''}" data-asset="${esc(asset.id)}" role="button" tabindex="0" draggable="true" aria-pressed="${selectedIds.has(asset.id)}" title="${esc(asset.name)} · 拖入创作区域，或点击选择"><input type="checkbox" class="ws-check" aria-label="选择 ${esc(asset.name)}" ${selectedIds.has(asset.id)?'checked':''}><span class="ws-thumb">${asset.kind==='image'?`<img draggable="false" src="${esc(asset.url)}" alt="" loading="lazy">`:asset.kind==='video'?`<video draggable="false" src="${esc(asset.url)}#t=0.05" preload="metadata" muted></video>`:'<span aria-hidden="true">♪</span>'}<span class="ws-kind">${({image:'图',video:'视频',audio:'音频'})[asset.kind]||'素材'}</span></span><span class="ws-asset-copy"><strong>${esc(asset.name)}</strong><span>${esc(mediaLabel(asset))}</span></span></article>`).join('') || `<p class="tray-empty">${query?'没有匹配的素材':'拖入文件、生成结果或历史素材'}</p>`;
    $('ws-assets').querySelectorAll('[data-asset]').forEach(card=>{card.onclick=event=>{const id=card.dataset.asset,multiple=event.ctrlKey||event.metaKey||event.shiftKey||event.target.matches('input');if(!multiple)selectedIds.clear();if(multiple&&selectedIds.has(id))selectedIds.delete(id);else selectedIds.add(id);selectedAsset=selectedIds.has(id)?id:[...selectedIds].at(-1)||null;render()};card.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();card.click()}};card.ondblclick=()=>{const asset=current.assets.find(item=>item.id===card.dataset.asset);useAsset(current.id,asset,asset.kind==='audio'?'reference':asset.kind==='video'||activeStage==='finish'?'finish':activeStage==='video'?'first':'edit')};});
    renderSelection();
  }
  function renderSelection() {
    const asset = current?.assets.find(item=>item.id===selectedAsset), target=$('ws-asset-selection');
    target.classList.toggle('hidden',!selectedIds.size); if(!selectedIds.size)return;
    if(selectedIds.size>1){target.innerHTML=`<strong>已选 ${selectedIds.size} 个素材</strong><div class="ws-row"><button class="danger small-button" data-remove>移除所选</button><button class="secondary small-button" data-clear-selection>取消选择</button></div>`;target.querySelector('[data-remove]').onclick=removeSelected;target.querySelector('[data-clear-selection]').onclick=()=>{selectedIds.clear();selectedAsset=null;render()};return;}
    if(!asset)return;
    const actions = asset.kind==='image' ? [['edit','修改图片'],['first','首帧'],['last','末帧'],['both','首尾帧'],['reference','参考'],['finish-reference','二采参考'],['finish','DLSS 滤镜'],['overlay','画面叠加']] : asset.kind==='video' ? [['finish','后处理'],['reference','视频参考'],['overlay','画面叠加']] : [['reference','音频参考']];
    target.innerHTML=`<strong>${esc(asset.name)}</strong><div class="ws-row">${actions.map(([id,label])=>`<button class="secondary small-button" data-use="${id}">${label}</button>`).join('')}<a class="secondary small-button" href="${esc(asset.url)}" download="${esc(asset.name)}">下载</a><button class="danger small-button" data-remove>移除</button></div>`;
    target.querySelectorAll('[data-use]').forEach(button=>button.onclick=()=>useAsset(current.id,asset,button.dataset.use));
    target.querySelector('[data-remove]').onclick=removeSelected;
  }
  function removeSelected(){if(!current||!selectedIds.size)return;const id=current.id,ids=[...selectedIds].filter(value=>current.assets.some(asset=>asset.id===value));confirm('移除所选素材',`仅从当前工作区移除这 ${ids.length} 个素材？历史版本仍可恢复，其他工作区不受影响。`,async guard=>{let updated;for(const assetId of ids){guard();updated=await api(`${base}/${id}/assets/${assetId}`,{method:'DELETE'});guard()}selectedIds.clear();selectedAsset=null;accept(updated);toast(`已移除 ${ids.length} 个素材`)})}
  function rememberTaskFile(file,url) {
    const match = String(url).match(/\/api\/(studio|video)\/tasks\/([a-f0-9]{32})\/assets\/([^/?]+)/);
    if (!match) return;
    const name=decodeURIComponent(match[3]);
    fileOrigins.set(file,{type:'task',kind:match[1]==='video'?'video':studioTasks.find(t=>t.id===match[2])?.kind||libraryTasks.find(t=>t.id===match[2])?.kind||(name==='image'||name.startsWith('input_image_')?'image':'postprocess'),task_id:match[2],asset_name:name});
  }
  async function assetFile(workspaceId,asset) {
    const file = await fileFromAsset(asset.url,asset.name);
    fileOrigins.set(file,{type:'workspace',workspace_id:workspaceId,asset_id:asset.id});
    return file;
  }
  function setWorkspaceVideo(workspaceId,asset) {
    clearFinishUpload();$('finish-task').value='';finishWorkspaceSource={workspaceId,assetId:asset.id};$('finish-source-name').textContent=asset.name;$('finish-clear').classList.remove('hidden');finishResultTask=null;$('finish-result').classList.add('hidden');$('finish-progress').classList.add('hidden');
    if(asset.kind==='image')selectRestoredOption('finish-method','dlss_nr','DLSS 5 滤镜');updateFinishPreview(asset.url,asset.kind,asset);
  }
  async function useAsset(workspaceId,asset,action) {
    return operation('载入工作区素材',async guard=>{
      assertTarget(asset.kind,action);
      if(action==='finish') { guard();setWorkspaceVideo(workspaceId,asset);openReuseStage('finish','素材已载入');return; }
      const file = await assetFile(workspaceId,asset); guard();
      applyFile(file,action,false);
    },true);
  }
  function assertTarget(kind,action){if(['finish','overlay'].includes(action)&&!['image','video'].includes(kind))throw Error('请选择图片或视频');if(['edit','first','last','both','title','finish-reference'].includes(action)&&kind!=='image')throw Error('这个位置只接收图片');if(action==='reference'&&!['image','video','audio'].includes(kind))throw Error('参考区支持图片、视频和音频');}
  function applyFile(file,action,keepPrompt=true){
    let stage='video';
    if(action==='title'&&!['image/png','image/webp'].includes(file.type)&&!(/\.(png|webp)$/i).test(file.name))throw Error('标题请选择 PNG 或 WebP 图片');
    if(action==='reference'){const groups=groupedReferenceFiles([...referenceFileBuffer,file]);if(groups.image.length>9||groups.video.length>3||groups.audio.length>3)throw Error('参考素材超过数量限制，请先移除部分素材');}
    if(action==='overlay'){setFiles($('finish-overlay-media'),[file]);$('finish-overlay-settings').open=true;stage='finish';}
    else if(action==='finish'){setFiles($('finish-source'),[file]);stage='finish';}
    else if(action==='edit'){clearImageDraftResult();setFiles($('image-source'),[file]);if(!keepPrompt){$('image-prompt').value='';$('image-seed').value=''}stage='image';}
    else if(action==='title'){setFiles($('finish-title-image'),[file]);stage='finish';}
    else if(action==='finish-reference'){if(!finishUsesReference()){const preset=['redgraft_msr'].find(id=>[...$('finish-method').options].some(option=>option.value===id&&!option.disabled));if(!preset)throw Error('LTX 二采环境未就绪');selectRestoredOption('finish-method',preset,preset);}setFiles($('finish-reference-image'),[file]);stage='finish';}
    else if(action==='reference'){if(mode==='text_frames')document.querySelector('[data-mode="hybrid"]').click();addReferenceFiles([file]);}
    else{if(mode==='full_reference')document.querySelector('[data-mode="hybrid"]').click();if(action!=='last')setFiles($('first-frame'),[file]);if(action==='last'||action==='both')setFiles($('last-frame'),[file]);$('frame-section').open=true;}
    openReuseStage(stage,'素材已载入');
  }
  function taskAssets(task) {
    const result=[];
    const labels={image:'生成的图片',video:task.kind==='postprocess'?'高清成片':'生成的视频',first_frame:'原始首帧',last_frame:'原始末帧',source_video:'原始处理视频',source_image:'原始处理图片',preview_source:'原始预览帧',overlay_media:'画面叠加素材',title_image:'旧叠加图片',reference_image:'参考图片'};
    const add=(name,url,label)=>{if(url&&!result.some(item=>item.name===name))result.push({name,url,label:label||labels[name]||name})};
    for(const [name,url] of Object.entries(task.assets||{})) {
      if(typeof url==='string' && /\.(png|jpg|jpeg|webp|mp4|mov|webm|wav|mp3|flac|m4a|ogg)(?:\?|$)/i.test(url)) add(name,url);
      else if(typeof url==='string' && (labels[name]||/^(input_image|reference_image|reference_video|reference_audio)_\d+$/.test(name)))add(name,url,labels[name]||({input_image:'原参考图',reference_image:'视频参考图',reference_video:'参考视频',reference_audio:'参考音频'})[name.replace(/_\d+$/,'')]+' '+(Number(name.match(/\d+$/)?.[0]||0)+(name.startsWith('input_image_')?1:0)));
    }
    for(const [index,input] of (task.input_images||[]).entries()) if(input.url)add(decodeURIComponent(input.url.split('/').pop().split('?')[0]),input.url,`原参考图 ${index+1} · ${input.filename||''}`);
    for(const [index,input] of (task.references||[]).entries()) if(input.url)add(decodeURIComponent(input.url.split('/').pop().split('?')[0]),input.url,`视频参考素材 ${index+1} · ${input.filename||''}`);
    const output = task.assets?.image?'image':'video';
    return result.sort((a,b)=>Number(b.name===output)-Number(a.name===output));
  }
  async function openTask(summary) {
    await open();
    return operation('读取任务素材',async guard=>{
      const kind=summary.kind||'video';
      const task=await api(kind==='video'?`/api/video/tasks/${summary.id}`:`/api/studio/tasks/${summary.id}`);guard();task.kind=kind;
      const assets=taskAssets(task);if(!assets.length)throw Error('这条任务没有可保存的素材');
      pendingTask={task,assets};$('ws-task-title').textContent=`任务素材 · ${(task.original_prompt||task.prompt||task.id).slice(0,30)}`;
      $('ws-task-asset').replaceChildren(...assets.map(asset=>new Option(asset.label,asset.name)),new Option(`全部 ${assets.length} 个输入与结果`,'__all'));
      $('ws-task-panel').classList.remove('hidden');render();
    });
  }
  function hideTask() { pendingTask=null; $('ws-task-panel').classList.add('hidden'); }
  function askName(action) {
    nameAction=action;$('ws-name-title').textContent=action==='new'?'新建工作区':'重命名工作区';
    $('ws-name').value=action==='new'?'':current?.name||'';$('ws-name-dialog').showModal();$('ws-name').focus();
  }
  function confirm(title,copy,action) {
    confirmAction=action;$('ws-confirm-title').textContent=title;$('ws-confirm-copy').textContent=copy;$('ws-confirm-dialog').showModal();
  }
  async function upload(files) {
    if(!files.length)return;if(!current){toast('请先选择或新建工作区，再拖入文件',true);return;}
    return operation('保存工作区素材',async guard=>{
      if([...files].some(file=>!referenceKind(file)))throw Error('请选择图片、视频或音频文件');
      const id=current.id,fd=new FormData();[...files].forEach(file=>fd.append('files',file));
      const updated=await api(`${base}/${id}/assets`,{method:'POST',body:fd});guard();accept(updated);toast(`已添加 ${files.length} 个素材`);
    });
  }
  async function loadVersions() {
    if(!current)return;
    const id=current.id;
    try {
      const response=await api(`${base}/${id}/history`);if(current?.id!==id)return;
      $('ws-revisions').innerHTML=(response.revisions||[]).map(rev=>`<div class="ws-revision"><span><strong>版本 ${rev.id}</strong><small>${esc(dateLabel(rev.created_at))} · ${rev.asset_count} 个素材</small></span><button class="secondary small-button" data-revision="${rev.id}">恢复</button></div>`).join('')||'<p class="field-caption">还没有保存记录。</p>';
      $('ws-revisions').querySelectorAll('[data-revision]').forEach(button=>button.onclick=()=>confirm('恢复保存版本',`将工作区恢复为版本 ${button.dataset.revision}。当前编辑器保持不变，恢复后可点击“载入工作区”。`,async guard=>{const updated=await api(`${base}/${id}/history/${button.dataset.revision}/restore`,{method:'POST'});guard();accept(updated);toast('已恢复工作区版本，点击载入可继续创作')}));
    } catch(error) { fail(error); }
  }

  const sessionDetails=[...document.querySelectorAll('.stage-panel details,#workspace-panel details')];
  sessionDetails.forEach((node,index)=>node.dataset.sessionDetail=node.id||`detail-${index}`);
  const sessionScrollNodes=[...document.querySelectorAll('.stage-panel .inspector-fields,.tray-body,.history-scroll')];
  sessionScrollNodes.forEach((node,index)=>node.dataset.sessionScroll=node.closest('.stage-panel')?.id||`pane-${index}`);
  function uiSnapshot(){return {details:Object.fromEntries(sessionDetails.map(node=>[node.dataset.sessionDetail,node.open])),scroll:Object.fromEntries(sessionScrollNodes.map(node=>[node.dataset.sessionScroll,node.scrollTop])),windowScroll:scrollY,drawerOpen:!collapsed,workspaceId:current?.id||null,workspaceQuery:$('ws-search').value,selectedAssets:[...selectedIds],historyOpen:historyPanel.classList.contains('history-open'),historyKind:libraryKind,historyQuery:libraryQuery,historySelected:librarySelected,historyPage,manualSources:[...manualSourceByMode.entries()],lastManualSource: lastManualSource?{...lastManualSource}:null,deliverySourceCustomized,previewTimes:Object.fromEntries(['image','video','finish'].map(stage=>[stage,stage===activeStage&&studioPreview[stage]?.type==='video'?$('workspace-preview-video').currentTime:0]))};}
  function snapshot() {
    const values=Object.fromEntries(fields.map(id=>[id,$(id).type==='checkbox'?$(id).checked:$(id).value]));
    return {stage:activeStage,video_mode:mode,fields:values,ui:uiSnapshot(),activeVideoTaskId:activeVideoPreviewTask,pendingStudioIds:[...studioPendingIds],
      files:{images:[...$('image-source').files],first:$('first-frame').files[0]||null,last:$('last-frame').files[0]||null,references:[...referenceFileBuffer],finish_video:$('finish-source').files[0]||null,finish_reference:$('finish-reference-image').files[0]||null,overlay:$('finish-overlay-media').files[0]||null,title:$('finish-title-image').files[0]||null},
      finishSource:finishWorkspaceSource?{type:'workspace',workspace_id:finishWorkspaceSource.workspaceId,asset_id:finishWorkspaceSource.assetId}:finishStudioSource?{type:'task',kind:finishStudioSource.kind||'postprocess',task_id:finishStudioSource.id,asset_name:finishStudioSource.asset}:$('finish-task').value?{type:'task',kind:'video',task_id:$('finish-task').value,asset_name:'video'}:null,
      results:Object.fromEntries(Object.entries(studioPreview).filter(([,state])=>state?.task?.status==='completed'||state?.workspaceAsset).map(([stage,state])=>[stage,state.workspaceAsset?{type:'workspace',workspace_id:state.workspaceAsset.workspaceId,asset_id:state.workspaceAsset.assetId}:{type:'task',kind:state.task.kind||(stage==='video'?'video':stage==='image'?'image':'postprocess'),task_id:state.task.id,asset_name:state.assetName||(state.type==='image'?'image':'video')}])),comparison:comparisonOrigin()};
  }
  function comparisonOrigin(){const state=studioPreview.finish;if(state?.comparison?.workspaceAsset)return{type:'workspace',workspace_id:state.comparison.workspaceAsset.workspaceId,asset_id:state.comparison.workspaceAsset.assetId};const task=state?.task;if(!finishComparisonAssets(task))return null;return{type:'task',kind:'postprocess',task_id:task.id,asset_name:state.type==='image'?(task.assets.preview_source?'preview_source':'source_image'):'source_video'};}
  function applyFormFields(values={},videoMode='hybrid',ui={}){
    $('video-auto-postprocess').checked=false;
    document.querySelector(`[data-mode="${['hybrid','full_reference','text_frames'].includes(videoMode)?videoMode:'hybrid'}"]`).click();
    for(const id of ['hybrid-preset','nsfw','hybrid-turbo'])if(id in values)selectRestoredOption(id,values[id],values[id],false);
    syncNsfwUi();updateHybridPreset();renderResolution(true);
    $('finish-dlss-controls-version').value=String(values['finish-dlss-controls-version']??(values['finish-method']==='dlss_nr'&&'finish-dlss-look'in values?1:2));
    $('finish-dlss-experimental').checked=!!values['finish-dlss-experimental'];
    for(const key of Object.keys(dlssParameterDefaults))$('finish-dlss-'+key).max=values['finish-dlss-experimental']?5:1;
    for(const id of fields){if(!(id in values)||['hybrid-preset','nsfw','hybrid-turbo'].includes(id))continue;const node=$(id);if(id==='aspect'){restoreSourceAspect(values[id]);continue}if(node.type==='checkbox')node.checked=!!values[id];else if(node.tagName==='SELECT'&&id!=='image-purpose')selectRestoredOption(id,values[id],values[id],false);else node.value=String(values[id]??'');}
    restoreAutoSource(values['video-auto-postprocess']===true);setDeliveryTarget(values['target-resolution']??'1080p');restoreSourceProfile(values['source-resolution-profile']??'');
    if(Array.isArray(ui.manualSources)){manualSourceByMode.clear();for(const [key,value]of ui.manualSources)if(['hybrid','full_reference','text_frames'].includes(key)&&value&&typeof value==='object')manualSourceByMode.set(key,{...value});lastManualSource=ui.lastManualSource?{...ui.lastManualSource}:null;deliverySourceCustomized=ui.deliverySourceCustomized!==false;}
    $('prompt').dispatchEvent(new Event('input'));$('finish-dlss-strength').dispatchEvent(new Event('input'));refreshFinishMode();refreshDeliveryInfo();updateWarnings();
  }
  function restorePreviewPosition(values={},ui={}){for(const id of ['finish-preview-frame','finish-preview-time'])if(id in values)$(id).value=String(values[id]??0);const state=studioPreview[activeStage],time=state?.task?Number(ui.previewTimes?.[activeStage]||0):activeStage==='finish'?Number(values['finish-preview-time']||0):Number(ui.previewTimes?.[activeStage]||0);if(state?.type==='video'&&time>0){const video=$('workspace-preview-video'),seek=()=>{video.currentTime=Math.min(time,Number.isFinite(video.duration)?video.duration:time)};if(video.readyState>=1)seek();else video.addEventListener('loadedmetadata',seek,{once:true});}}
  async function applyUiState(ui={},stage='video'){
    for(const node of sessionDetails)if(node.dataset.sessionDetail in(ui.details||{}))node.open=!!ui.details[node.dataset.sessionDetail];
    collapsed=ui.drawerOpen!==true;applyTraySize();$('ws-search').value=ui.workspaceQuery??'';selectedIds.clear();for(const id of ui.selectedAssets||[])if(current?.assets.some(asset=>asset.id===id))selectedIds.add(id);selectedAsset=[...selectedIds][0]||null;render();
    libraryKind=['all','image','video','postprocess'].includes(ui.historyKind)?ui.historyKind:'all';libraryQuery=ui.historyQuery??'';librarySelected=ui.historySelected??null;historyPage=Math.max(0,Number(ui.historyPage)||0);$('library-search').value=libraryQuery;document.querySelectorAll('[data-library-kind]').forEach(node=>{const active=node.dataset.libraryKind===libraryKind;node.classList.toggle('active',active);node.setAttribute('aria-pressed',String(active))});
    setStage(['image','video','finish'].includes(stage)?stage:'video');setHistoryOpen(ui.historyOpen===true);if(ui.historyOpen)await loadLibrary(true);
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    for(const node of sessionScrollNodes)node.scrollTop=Number(ui.scroll?.[node.dataset.sessionScroll])||0;scrollTo(0,Number(ui.windowScroll)||0);
  }
  let sessionDatabase=null,sessionLoadedOwner='',sessionRestoreEpoch=0,sessionRestoring=false,sessionSaveTimer=null,sessionWriteQueue=Promise.resolve(),sessionFileIds=new WeakMap(),sessionBaseline=null,sessionSaveErrorShown=false;
  const sessionOwner=()=>accessInfo?.authenticated?JSON.stringify([accessInfo.role,accessInfo.key_id??accessInfo.name??accessInfo.role]):'';
  const sessionDraftKey=owner=>'frame-studio-session-form:'+owner;
  const sessionFileSignature=file=>file?{name:file.name,size:file.size,type:file.type,lastModified:file.lastModified}:null;
  function readSessionDraft(owner){try{const value=JSON.parse(localStorage.getItem(sessionDraftKey(owner)));return value?.version===1&&value.form?value:null}catch{return null}}
  function writeSessionDraft(owner,captured,savedAt){const {stage,video_mode,fields,ui,finishSource,results,comparison,activeVideoTaskId,pendingStudioIds}=captured,files=Object.fromEntries(Object.entries(captured.files).map(([name,value])=>[name,Array.isArray(value)?value.map(sessionFileSignature):sessionFileSignature(value)]));try{localStorage.setItem(sessionDraftKey(owner),JSON.stringify({version:1,savedAt,form:{stage,video_mode,fields,ui,finishSource,results,comparison,activeVideoTaskId,pendingStudioIds},files}))}catch{/* Full snapshots and files still use the atomic IndexedDB transaction. */}}
  function localDatabase(){if(!sessionDatabase)sessionDatabase=new Promise((resolve,reject)=>{const request=indexedDB.open('frame-studio-session',1);request.onupgradeneeded=()=>{request.result.createObjectStore('sessions');request.result.createObjectStore('media')};request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error)});return sessionDatabase;}
  async function readLocalSession(owner){const db=await localDatabase();return new Promise((resolve,reject)=>{const tx=db.transaction(['sessions','media'],'readonly'),request=tx.objectStore('sessions').get(owner),media=new Map();let record=null;request.onsuccess=()=>{record=request.result;if(record?.version!==1)return;for(const id of record.blobKeys||[]){if(!id.startsWith(owner+'|'))continue;const get=tx.objectStore('media').get(id);get.onsuccess=()=>media.set(id,get.result)}};tx.oncomplete=()=>resolve({record,media});tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error)});}
  async function writeLocalSession(owner,captured,savedAt){const db=await localDatabase(),media=new Map(),keys=new Set(),fileKey=file=>{if(!file)return null;let entry=sessionFileIds.get(file);if(entry?.owner!==owner){entry={owner,id:owner+'|'+crypto.randomUUID()};sessionFileIds.set(file,entry)}keys.add(entry.id);media.set(entry.id,{file,name:file.name,type:file.type,lastModified:file.lastModified,owner});return entry.id},files=Object.fromEntries(Object.entries(captured.files).map(([name,value])=>[name,Array.isArray(value)?value.map(fileKey):fileKey(value)])),record={version:1,savedAt,blobKeys:[...keys],snapshot:{...captured,files}};return new Promise((resolve,reject)=>{const tx=db.transaction(['sessions','media'],'readwrite'),sessions=tx.objectStore('sessions'),blobs=tx.objectStore('media'),previous=sessions.get(owner);previous.onsuccess=()=>{const previousKeys=new Set(previous.result?.blobKeys||[]);for(const[id,value]of media)if(!previousKeys.has(id))blobs.put(value,id);for(const id of previousKeys)if(id.startsWith(owner+'|')&&!keys.has(id))blobs.delete(id);sessions.put(record,owner)};tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error)});}
  function flushSession(){clearTimeout(sessionSaveTimer);const owner=sessionOwner();if(!owner||owner!==sessionLoadedOwner||sessionRestoring)return sessionWriteQueue;const captured=snapshot(),savedAt=Date.now();writeSessionDraft(owner,captured,savedAt);sessionWriteQueue=sessionWriteQueue.catch(()=>{}).then(()=>writeLocalSession(owner,captured,savedAt)).catch(error=>{if(!sessionSaveErrorShown){sessionSaveErrorShown=true;toast(error?.name==='QuotaExceededError'?'本地空间不足，请将素材保存到工作区':'自动保存失败，请将素材保存到工作区',true)}throw error});return sessionWriteQueue;}
  function scheduleSessionSave(){if(sessionRestoring||!sessionLoadedOwner||sessionLoadedOwner!==sessionOwner())return;clearTimeout(sessionSaveTimer);sessionSaveTimer=setTimeout(()=>flushSession().catch(()=>{}),250);}
  async function restoreSession(){
    const owner=sessionOwner();if(!owner||!studioCapabilities||sessionLoadedOwner===owner||sessionRestoring)return;
    if(!sessionBaseline)sessionBaseline={fields:snapshot().fields,video_mode:mode};
    const epoch=++sessionRestoreEpoch,guard=()=>{if(epoch!==sessionRestoreEpoch||sessionOwner()!==owner){const error=Error('身份已更换');error.name='ReuseCancelled';throw error}};
    sessionRestoring=true;let missing=false;
    try{
      let {record,media}=await readLocalSession(owner);guard();const draft=readSessionDraft(owner),useDraft=draft&&draft.savedAt>=(record?.savedAt??0);if(useDraft)record={version:1,snapshot:{...(record?.snapshot||{}),...draft.form}};
      if(record?.version!==1||!record.snapshot){sessionLoadedOwner=owner;return}
      const captured=record.snapshot,files={},fileFromKey=id=>{if(!id)return null;const entry=media.get(id);if(!entry?.file||entry.owner!==owner){missing=true;return null}const file=entry.file instanceof File?entry.file:new File([entry.file],entry.name||'material',{type:entry.type||entry.file.type,lastModified:entry.lastModified||Date.now()});sessionFileIds.set(file,{owner,id});return file};
      for(const[name,value]of Object.entries(captured.files||{}))files[name]=Array.isArray(value)?value.map(fileFromKey).filter(Boolean):fileFromKey(value);
      if(useDraft&&draft.files)for(const[name,signatures]of Object.entries(draft.files)){const available=Array.isArray(files[name])?files[name]:files[name]?[files[name]]:[],match=signature=>{if(!signature)return null;const file=available.find(file=>['name','size','type','lastModified'].every(key=>sessionFileSignature(file)[key]===signature[key]));if(!file)missing=true;return file||null};files[name]=Array.isArray(signatures)?signatures.map(match).filter(Boolean):match(signatures)}
      const resolved={};for(const[key,origin]of Object.entries({finishSource:captured.finishSource,comparison:captured.comparison,...Object.fromEntries(Object.entries(captured.results||{}).map(([stage,origin])=>['result-'+stage,origin]))})){if(!origin)continue;try{const source=await resolvePayload(origin,guard);const response=await fetch(source.asset.url,{headers:{Range:'bytes=0-0'},cache:'no-store'});if(!response.ok)throw Error('素材不可用');await response.body?.cancel();guard();resolved[key]=source}catch(error){guard();missing=true}}
      await refreshList();guard();if(captured.ui?.workspaceId&&libraries.some(item=>item.id===captured.ui.workspaceId))await select(captured.ui.workspaceId);guard();
      applyFormFields(captured.fields,captured.video_mode,captured.ui||{});
      for(const stage of ['image','video','finish'])setStudioPreview(stage,null);
      setFiles($('image-source'),files.images||[]);if(files.first)setFiles($('first-frame'),[files.first]);else clearFrame('first');if(files.last)setFiles($('last-frame'),[files.last]);else clearFrame('last');addReferenceFiles(files.references||[],true);
      clearFinishUpload();$('finish-task').value='';updateFinishPreview(null);
      if(files.finish_video)setFiles($('finish-source'),[files.finish_video]);else if(resolved.finishSource){const source=resolved.finishSource;if(source.workspaceId)setWorkspaceVideo(source.workspaceId,source.asset);else if(source.task.kind==='video'&&source.payload.asset_name==='video'){refreshFinishSources([source.task]);$('finish-task').value=source.task.id;updateFinishPreview(source.asset.url,source.asset.kind,source.task)}else setStudioSource(source.task,source.payload.asset_name)}
      setFiles($('finish-reference-image'),files.finish_reference?[files.finish_reference]:[]);setFiles($('finish-title-image'),files.title?[files.title]:[]);setFiles($('finish-overlay-media'),files.overlay?[files.overlay]:[]);$('finish-overlay-enabled').checked=!!captured.fields?.['finish-overlay-enabled']&&!!files.overlay;
      for(const stage of ['image','video','finish']){const source=resolved['result-'+stage];if(!source)continue;const state={url:source.asset.url,type:source.asset.kind,label:source.asset.name||'已恢复结果'};if(source.workspaceId)state.workspaceAsset={workspaceId:source.workspaceId,assetId:source.asset.id};else{state.task=source.task;state.assetName=source.payload.asset_name}if(stage==='finish'&&resolved.comparison?.workspaceId)state.comparison={url:resolved.comparison.asset.url,workspaceAsset:{workspaceId:resolved.comparison.workspaceId,assetId:resolved.comparison.asset.id}};setStudioPreview(stage,state)}
      if(/^[a-f0-9]{32}$/.test(captured.activeVideoTaskId||''))try{const task=await api(`/api/video/tasks/${captured.activeVideoTaskId}`);guard();activeVideoPreviewTask=task.id;rememberTask(task.id);renderActiveVideoProgress(task)}catch(error){guard()}
      for(const id of (captured.pendingStudioIds||[]).filter(id=>/^[a-f0-9]{32}$/.test(id)))try{const task=await api(`/api/studio/tasks/${id}`);guard();await trackStudioTask(task)}catch(error){guard()}
      refreshOverlayControls();refreshFinishMode();await applyUiState(captured.ui||{},captured.stage);guard();restorePreviewPosition(captured.fields,captured.ui||{});sessionLoadedOwner=owner;if(missing)toast('部分素材已不可用，请重新选择',true);
    }catch(error){if(error.name!=='ReuseCancelled'){sessionLoadedOwner=owner;toast('上次编辑未能完整恢复，请从工作区载入',true)}}finally{if(epoch===sessionRestoreEpoch)sessionRestoring=false}
  }
  function resetSession(){flushSession().catch(()=>{});sessionRestoreEpoch++;sessionRestoring=false;sessionLoadedOwner='';clearTimeout(sessionSaveTimer);sessionFileIds=new WeakMap();if(sessionBaseline)applyFormFields(sessionBaseline.fields,sessionBaseline.video_mode);for(const node of sessionDetails)node.open=false;setStage('video');}
  for(const eventName of ['input','change','click','drop'])document.addEventListener(eventName,event=>{if(event.target?.closest?.('main,#workspace-panel,header .stage-nav,#history-toggle'))scheduleSessionSave()},true);
  document.addEventListener('toggle',event=>{if(sessionDetails.includes(event.target))scheduleSessionSave()},true);
  document.addEventListener('scroll',event=>{if(sessionScrollNodes.includes(event.target))scheduleSessionSave()},true);addEventListener('scroll',scheduleSessionSave,{passive:true});
  addEventListener('pagehide',()=>flushSession().catch(()=>{}));document.addEventListener('visibilitychange',()=>{if(document.hidden)flushSession().catch(()=>{})});
  async function saveDraft() {
    if(!current)return;
    const captured=snapshot(),workspaceId=current.id;
    return operation('保存当前创作与素材',async guard=>{
      let detail=await api(`${base}/${workspaceId}`);guard();
      const resolvedFiles=new Map(),resolvedOrigins=new Map();
      const originKey=origin=>JSON.stringify(origin.type==='task'?['task',origin.kind,origin.task_id,origin.asset_name]:['workspace',origin.workspace_id,origin.asset_id]);
      async function resolveOrigin(origin) {
        const key=originKey(origin);if(resolvedOrigins.has(key))return resolvedOrigins.get(key);
        let existing=origin.type==='workspace'&&origin.workspace_id===workspaceId?detail.assets.find(asset=>asset.id===origin.asset_id):detail.assets.find(asset=>origin.type==='task'?asset.source?.task_id===origin.task_id&&asset.source?.asset_name===origin.asset_name:asset.source?.workspace_id===origin.workspace_id&&asset.source?.asset_id===origin.asset_id);
        if(existing){resolvedOrigins.set(key,existing.id);return existing.id;}
        guard();
        detail=await api(`${base}/${workspaceId}/${origin.type==='task'?'from-task':'from-workspace'}`,json(origin.type==='task'?{kind:origin.kind,task_id:origin.task_id,asset_name:origin.asset_name}:{source_workspace_id:origin.workspace_id,asset_id:origin.asset_id}));
        guard();const id=detail.added_asset_ids?.[0];if(!id)throw Error('服务器未返回新素材编号，请重新打开工作区检查');resolvedOrigins.set(key,id);return id;
      }
      const allFiles=[...captured.files.images,captured.files.first,captured.files.last,...captured.files.references,captured.files.finish_video,captured.files.finish_reference,captured.files.overlay,captured.files.title].filter(Boolean);
      const unique=[...new Set(allFiles)],unknown=[];
      for(const file of unique){const origin=fileOrigins.get(file);if(origin)resolvedFiles.set(file,await resolveOrigin(origin));else unknown.push(file);}
      if(unknown.length){const fd=new FormData();unknown.forEach(file=>fd.append('files',file));guard();detail=await api(`${base}/${workspaceId}/assets`,{method:'POST',body:fd});guard();if(detail.added_asset_ids?.length!==unknown.length)throw Error('素材数量与服务器返回不一致，请检查工作区后重试');unknown.forEach((file,index)=>{const id=detail.added_asset_ids[index];resolvedFiles.set(file,id);fileOrigins.set(file,{type:'workspace',workspace_id:workspaceId,asset_id:id})});}
      const get=file=>file?resolvedFiles.get(file):null;
      const inputs={images:captured.files.images.map(get),first:get(captured.files.first),last:get(captured.files.last),references:captured.files.references.map(get),finish_video:get(captured.files.finish_video),finish_reference:get(captured.files.finish_reference),overlay:get(captured.files.overlay),title:get(captured.files.title)};
      if(!inputs.finish_video&&captured.finishSource)inputs.finish_video=await resolveOrigin(captured.finishSource);
      if(!inputs.finish_reference&&captured.finishSource?.type==='task'){
        const origin=captured.finishSource,task=await api(origin.kind==='video'?`/api/video/tasks/${origin.task_id}`:`/api/studio/tasks/${origin.task_id}`);guard();
        const name=(origin.kind==='video'?['start_frame','first_frame','reference_image_1']:['reference_image']).find(name=>task.assets?.[name]);
        if(name)inputs.finish_reference=await resolveOrigin({...origin,asset_name:name});
      }
      const results={};for(const [stage,origin] of Object.entries(captured.results))results[stage]=await resolveOrigin(origin);
      const comparison= captured.comparison?await resolveOrigin(captured.comparison):null;const draft={schema_version:1,stage:captured.stage,video_mode:captured.video_mode,fields:captured.fields,ui:captured.ui,inputs,results,comparison};
      guard();detail=await api(`${base}/${workspaceId}/save`,json({draft}));guard();accept(detail);toast(`已保存“${detail.name}” · 版本 ${detail.revision}`);
    });
  }
  async function loadDraft() {
    if(!current)return;
    const workspaceId=current.id;
    return operation('载入工作区与素材',async guard=>{
      const detail=await api(`${base}/${workspaceId}`);guard();
      const draft=detail.draft;if(!draft?.schema_version)throw Error('这个工作区尚未保存创作参数');
      if(draft.schema_version!==1)throw Error('这个工作区使用了较新的草稿格式，请升级工作台');
      const inputs=draft.inputs||{},assetMap=new Map(detail.assets.map(asset=>[asset.id,asset]));
      const allIds=[...(inputs.images||[]),inputs.first,inputs.last,...(inputs.references||[]),inputs.finish_video,inputs.finish_reference,inputs.overlay,inputs.title,draft.comparison,...Object.values(draft.results||{})].filter(Boolean);
      if(allIds.some(id=>!assetMap.has(id)))throw Error('草稿关联的素材已被移除。请先恢复包含素材的保存版本。');
      const fileIds=[...(inputs.images||[]),inputs.first,inputs.last,...(inputs.references||[]),inputs.finish_reference,inputs.overlay,inputs.title].filter(Boolean);
      const entries=await Promise.all([...new Set(fileIds)].map(async id=>[id,await assetFile(workspaceId,assetMap.get(id))]));guard();
      const fileMap=new Map(entries),files=ids=>(ids||[]).map(id=>fileMap.get(id));
      if((inputs.images||[]).length>4)throw Error('草稿参考图片超过当前上限');
      const refGroups=groupedReferenceFiles(files(inputs.references));if(refGroups.image.length>9||refGroups.video.length>3||refGroups.audio.length>3)throw Error('草稿参考素材数量超过当前上限');
      if(inputs.finish_video&&!['image','video'].includes(assetMap.get(inputs.finish_video).kind))throw Error('草稿处理源不是图片或视频');
      if(inputs.finish_reference&&assetMap.get(inputs.finish_reference).kind!=='image')throw Error('草稿中的二采参考不是图片');
      // Everything needed has been fetched and checked. Apply only after the shared guard.
      guard();
      clearImageDraftResult();imageResultTask=null;finishResultTask=null;activeVideoPreviewTask=null;
      for(const id of ['image-progress','video-progress','finish-progress','image-result','finish-result'])$(id).classList.add('hidden');
      const values=draft.fields||{};applyFormFields(values,draft.video_mode,draft.ui||{});
      $('image-purpose-hint').classList.toggle('hidden',!imageConcepts[$('image-purpose').value]);
      if(['redgraft_loop','rtx_loop'].includes($('finish-method').value))$('finish-loop').checked=true;
      setFiles($('image-source'),files(inputs.images));
      if(inputs.first)setFiles($('first-frame'),[fileMap.get(inputs.first)]);else clearFrame('first');
      if(inputs.last)setFiles($('last-frame'),[fileMap.get(inputs.last)]);else clearFrame('last');
      addReferenceFiles(files(inputs.references),true);
      clearFinishUpload();$('finish-task').value='';updateFinishPreview(null);
      if(inputs.finish_video)setWorkspaceVideo(workspaceId,assetMap.get(inputs.finish_video));
      setFiles($('finish-reference-image'),inputs.finish_reference?[fileMap.get(inputs.finish_reference)]:[]);
      setFiles($('finish-title-image'),inputs.title?[fileMap.get(inputs.title)]:[]);setFiles($('finish-overlay-media'),inputs.overlay?[fileMap.get(inputs.overlay)]:[]);$('finish-overlay-enabled').checked=!!values['finish-overlay-enabled']&&!!inputs.overlay;refreshOverlayControls();refreshFinishMode();
      $('prompt').dispatchEvent(new Event('input'));syncNsfwUi();updateWarnings();
      for(const [stage,id] of Object.entries(draft.results||{})){const asset=assetMap.get(id);if(['image','video','finish'].includes(stage)&&['image','video'].includes(asset.kind))setStudioPreview(stage,{url:asset.url,type:asset.kind,label:`${detail.name} · ${asset.name}`,workspaceAsset:{workspaceId,assetId:id},...(stage==='finish'&&draft.comparison?{comparison:{url:assetMap.get(draft.comparison).url,workspaceAsset:{workspaceId,assetId:draft.comparison}}}:{})});}
      accept(detail);await applyUiState(draft.ui||{},draft.stage);restorePreviewPosition(values,draft.ui||{});showReuseNotice(draft.stage||'video',`已载入“${detail.name}”`);scheduleSessionSave();
      if(values.nsfw&&values.nsfw!=='off'&&!nsfwAvailable())showReuseNotice('video','原 NSFW 方案已退役，请在综合模式选择 NSFW 特调后继续。');
    },true);
  }

  // Drag payloads contain only public IDs. Resolve every payload again through owner-scoped APIs.
  function validPayload(value){
    if(!value||typeof value!=='object'||!['workspace','task'].includes(value.type))throw Error('无法识别拖入的素材');
    const id=value=>typeof value==='string'&&/^[a-f0-9]{32}$/.test(value);
    if(value.type==='workspace'){if(!id(value.workspace_id)||!id(value.asset_id))throw Error('素材编号无效');return{type:'workspace',workspace_id:value.workspace_id,asset_id:value.asset_id,kind:value.kind};}
    if(!id(value.task_id)||!['image','video','postprocess'].includes(value.kind)||!(/^[a-z][a-z0-9_]{0,79}$/).test(value.asset_name||''))throw Error('任务素材编号无效');
    return{type:'task',task_id:value.task_id,kind:value.kind,asset_name:value.asset_name,media_kind:value.media_kind};
  }
  function payloadKind(payload){return payload.type==='workspace'?payload.kind:payload.media_kind||inferKind(payload.asset_name);}
  function inferKind(name){if(name==='video'||name==='source_video'||name.startsWith('reference_video'))return'video';if(name.startsWith('reference_audio'))return'audio';return'image';}
  function payloadFromUrl(url){const match=String(url||'').match(/\/api\/(studio|video)\/tasks\/([a-f0-9]{32})\/assets\/([a-z0-9_]+)/);if(!match)return null;const name=match[3];return{type:'task',task_id:match[2],kind:match[1]==='video'?'video':studioTasks.find(t=>t.id===match[2])?.kind||libraryTasks.find(t=>t.id===match[2])?.kind||(name==='image'||name.startsWith('input_image')?'image':'postprocess'),asset_name:name,media_kind:inferKind(name)};}
  async function resolvePayload(raw,guard){
    const payload=validPayload(raw);
    if(payload.type==='workspace'){const detail=await api(`${base}/${payload.workspace_id}`);guard();const asset=detail.assets.find(asset=>asset.id===payload.asset_id);if(!asset)throw Error('这个素材已被移除，请刷新工作区');return{payload,asset,workspaceId:detail.id};}
    const task=await api(payload.kind==='video'?`/api/video/tasks/${payload.task_id}`:`/api/studio/tasks/${payload.task_id}`);guard();task.kind=payload.kind;
    const source=taskAssets(task).find(asset=>asset.name===payload.asset_name);if(!source)throw Error('原任务素材已经不可用');
    const input=[...(task.references||[]),...(task.input_images||[])].find(input=>input.url===source.url);
    return{payload,task,asset:{id:payload.asset_name,url:source.url,kind:source.name==='overlay_media'?(task.overlay_media_kind||task.overlay_options?.media_kind||'image'):inferKind(source.name),name:source.label||source.name,filename:input?.filename}};
  }
  async function storePayload(payload){
    if(!current){toast('请先选择或新建一个工作区',true);collapsed=false;applyTraySize();return;}
    const destination=current.id;
    return operation('收纳拖入的素材',async guard=>{
      const source=await resolvePayload(payload,guard);guard();
      if(source.workspaceId===destination){toast('素材已经在当前工作区');return;}
      const detail=await api(`${base}/${destination}/${source.payload.type==='task'?'from-task':'from-workspace'}`,json(source.payload.type==='task'?{kind:source.payload.kind,task_id:source.payload.task_id,asset_name:source.payload.asset_name}:{source_workspace_id:source.workspaceId,asset_id:source.asset.id}));guard();
      selectedIds.clear();const id=detail.added_asset_ids?.[0];if(id){selectedIds.add(id);selectedAsset=id}accept(detail);collapsed=false;applyTraySize();toast('素材已收纳，原素材仍保留');
    });
  }
  async function dropPayload(payload,action){
    return operation('载入拖入的素材',async guard=>{
      const source=await resolvePayload(payload,guard);assertTarget(source.asset.kind,action);guard();
      if(action==='finish'){
        if(source.workspaceId)setWorkspaceVideo(source.workspaceId,source.asset);
        else if(source.task.kind==='video'&&source.payload.asset_name==='video'){sendVideoToFinish(source.task);}
        else if(['image','postprocess'].includes(source.task.kind)&&['image','source_image','video','source_video'].includes(source.payload.asset_name)){setStudioSource(source.task,source.payload.asset_name);}
        else{if(!current)throw Error('请先把这个参考视频存入工作区，再拖入高清处理');const detail=await api(`${base}/${current.id}/from-task`,json({kind:source.payload.kind,task_id:source.payload.task_id,asset_name:source.payload.asset_name}));guard();const asset=detail.assets.find(item=>item.id===detail.added_asset_ids?.[0]);if(!asset)throw Error('无法读取已复制的视频');accept(detail);setWorkspaceVideo(detail.id,asset);}
        openReuseStage('finish','素材已载入');return;
      }
      let file;if(source.workspaceId)file=await assetFile(source.workspaceId,source.asset);else{file=await fileFromAsset(source.asset.url,source.asset.filename||source.payload.asset_name);if(!source.asset.filename){const extensions={'image/png':'png','image/jpeg':'jpg','image/webp':'webp','video/mp4':'mp4','video/webm':'webm','video/quicktime':'mov','audio/mpeg':'mp3','audio/wav':'wav','audio/x-wav':'wav','audio/aac':'aac','audio/ogg':'ogg','audio/flac':'flac','audio/mp4':'m4a'};const extension=extensions[file.type.split(';')[0]];if(extension){file=new File([file],`${source.payload.asset_name}.${extension}`,{type:file.type});rememberTaskFile(file,source.asset.url)}}}guard();applyFile(file,action,true);
    },true);
  }
  const dropTargets=[['image-drop','edit'],['first-drop','first'],['last-drop','last'],['reference-drop','reference'],['finish-drop','finish'],['finish-reference-drop','finish-reference'],['finish-overlay-drop','overlay']];
  function targetAction(element,payload){const target=element?.closest?.('#image-drop,#first-drop,#last-drop,#reference-drop,#finish-drop,#finish-reference-drop,#finish-overlay-drop,[data-stage]');if(!target)return null;if(target.dataset.stage)return{node:target,action:target.dataset.stage==='image'?'edit':target.dataset.stage==='video'?'first':'finish'};return{node:target,action:dropTargets.find(([id])=>id===target.id)?.[1]};}
  function canTarget(kind,action){return action==='reference'?['image','video','audio'].includes(kind):['finish','overlay'].includes(action)?['image','video'].includes(kind):kind==='image';}
  function markTargets(payload){clearHighlights();const kind=payloadKind(payload);for(const[id,action]of dropTargets){const element=$(id);if(element.getClientRects().length&&canTarget(kind,action))element.classList.add('studio-drop-ready')}for(const tab of document.querySelectorAll('[data-stage]')){const action=tab.dataset.stage==='image'?'edit':tab.dataset.stage==='video'?'first':'finish';if(canTarget(kind,action))tab.classList.add('studio-drop-ready')}panel.classList.add('studio-drop-ready');}
  function clearHighlights(){document.querySelectorAll('.studio-drop-ready,.studio-drop-over,.studio-drop-invalid').forEach(node=>node.classList.remove('studio-drop-ready','studio-drop-over','studio-drop-invalid'));panel.classList.remove('dragging');}
  function dragSource(event){
    const card=event.target.closest?.('#ws-assets [data-asset]');if(card&&current){const asset=current.assets.find(item=>item.id===card.dataset.asset);return asset?{type:'workspace',workspace_id:current.id,asset_id:asset.id,kind:asset.kind}:null;}
    const historyInput=event.target.closest?.('[data-drag-task]');if(historyInput)return{type:'task',task_id:historyInput.dataset.dragTask,kind:historyInput.dataset.dragKind,asset_name:historyInput.dataset.dragAsset,media_kind:historyInput.dataset.dragMedia||inferKind(historyInput.dataset.dragAsset)};
    const comparison=event.target.closest?.('[data-comparison-side]'),comparisonState=studioPreview.finish;if(comparison&&activeStage==='finish'&&comparisonState?.workspaceAsset){const origin=comparison.dataset.comparisonSide==='source'?comparisonState.comparison?.workspaceAsset:comparisonState.workspaceAsset;if(origin)return{type:'workspace',workspace_id:origin.workspaceId,asset_id:origin.assetId,kind:comparisonState.type}}if(comparison&&activeStage==='finish'&&comparisonState?.task){const task=comparisonState.task,kind=comparisonState.type,asset_name=comparison.dataset.comparisonSide==='source'?(kind==='image'?(task.assets.preview_source?'preview_source':'source_image'):'source_video'):kind;return{type:'task',task_id:task.id,kind:task.kind||'postprocess',asset_name,media_kind:kind};}
    if(event.target.closest?.('#preview-canvas,#workspace-preview-image,#workspace-preview-video')){const state=studioPreview[activeStage];if(state?.workspaceAsset)return{type:'workspace',workspace_id:state.workspaceAsset.workspaceId,asset_id:state.workspaceAsset.assetId,kind:state.type};if(state?.task)return{type:'task',task_id:state.task.id,kind:state.task.kind||(activeStage==='video'?'video':activeStage==='image'?'image':'postprocess'),asset_name:state.assetName||(state.type==='image'?'image':'video'),media_kind:state.type};}
    const history=event.target.closest?.('[data-library-id]');if(history){const task=libraryTasks.find(item=>item.id===history.dataset.libraryId);if(!task)return null;const asset_name=task.assets?.video?'video':task.assets?.image?'image':task.assets?.first_frame?'first_frame':null;return asset_name?{type:'task',task_id:task.id,kind:task.kind||'video',asset_name,media_kind:inferKind(asset_name)}:null;}
    const media=event.target.closest?.('#detail img,#detail video,#detail audio');return media?payloadFromUrl(media.getAttribute('src')):null;
  }
  function wireDragSources(){document.querySelectorAll('#workspace-preview-image,#workspace-preview-video,#finish-split-preview img,#finish-split-preview video,[data-library-id],#detail img,#detail video,#detail audio').forEach(node=>{node.draggable=true;node.classList.add('studio-drag-source');node.title=node.title||'拖入左侧素材栏保存'});}
  function renderHistoryInputs(task){const host=$('library-selection');host.querySelector('.history-input-strip')?.remove();const items=taskAssets(task).filter(asset=>!['image','video'].includes(asset.name));if(!items.length)return;const strip=document.createElement('div');strip.className='history-input-strip';strip.innerHTML=`<span>原输入</span><div>${items.map(asset=>{const kind=asset.name==='overlay_media'?(task.overlay_media_kind||task.overlay_options?.media_kind||'image'):inferKind(asset.name);return`<div class="history-input-asset" draggable="true" role="button" tabindex="0" data-drag-task="${esc(task.id)}" data-drag-kind="${esc(task.kind||'video')}" data-drag-asset="${esc(asset.name)}" data-drag-media="${kind}" title="${esc(asset.label)} · 拖入素材栏保存">${kind==='image'?`<img draggable="false" src="${esc(asset.url)}" alt="${esc(asset.label)}">`:kind==='video'?`<video draggable="false" muted preload="metadata" src="${esc(asset.url)}"></video>`:'<span>音频</span>'}<small>${esc(asset.label)}</small></div>`}).join('')}</div>`;host.append(strip);strip.querySelectorAll('[data-drag-task]').forEach(node=>{node.ondblclick=()=>storePayload({type:'task',task_id:task.id,kind:task.kind||'video',asset_name:node.dataset.dragAsset});node.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();node.ondblclick()}}});}
  let pointerDrag=null,suppressClickUntil=0,suppressClickPoint=null;
  function cancelPointerDrag(){pointerDrag=null;draggingPayload=null;clearTimeout(hoverTimer);clearHighlights();}
  document.addEventListener('dragstart',event=>{const payload=dragSource(event);if(!payload)return;draggingPayload=validPayload(payload);event.dataTransfer.setData(mime,JSON.stringify(draggingPayload));if(pointerDrag&&!event.altKey){event.preventDefault();markTargets(draggingPayload);return}if(pointerDrag)pointerDrag.native=true;event.dataTransfer.effectAllowed='copy';event.stopPropagation();markTargets(draggingPayload);},true);
  document.addEventListener('dragend',cancelPointerDrag,true);
  document.addEventListener('dragover',event=>{
    const custom=[...event.dataTransfer.types].includes(mime);if(!custom&&!draggingPayload)return;event.preventDefault();event.dataTransfer.dropEffect='copy';
    if(event.target.closest?.('#workspace-panel')){event.preventDefault();event.stopPropagation();event.dataTransfer.dropEffect='copy';panel.classList.add('dragging');if(collapsed){collapsed=false;applyTraySize()}return;}
    const target=targetAction(event.target,draggingPayload);if(!target)return;event.preventDefault();event.stopPropagation();event.dataTransfer.dropEffect='copy';target.node.classList.add(!draggingPayload||canTarget(payloadKind(draggingPayload),target.action)?'studio-drop-over':'studio-drop-invalid');
  },true);
  document.addEventListener('dragenter',event=>{const tab=event.target.closest?.('[data-stage]');if(!tab||!draggingPayload)return;clearTimeout(hoverTimer);const stage=tab.dataset.stage;hoverTimer=setTimeout(()=>{if(!draggingPayload)return;setStage(stage);markTargets(draggingPayload);},450);},true);
  document.addEventListener('dragleave',event=>{const target=targetAction(event.target,draggingPayload);if(target&&!target.node.contains(event.relatedTarget))target.node.classList.remove('studio-drop-over','studio-drop-invalid');if(event.target.closest?.('[data-stage]'))clearTimeout(hoverTimer);if(!panel.contains(event.relatedTarget)&&panel.contains(event.target))panel.classList.remove('dragging');},true);
  document.addEventListener('drop',event=>{
    if(![...event.dataTransfer.types].includes(mime))return;
    const tray=event.target.closest?.('#workspace-panel'),target=targetAction(event.target,draggingPayload);if(!tray&&!target)return;
    event.preventDefault();event.stopImmediatePropagation();clearTimeout(hoverTimer);clearHighlights();
    try{const raw=event.dataTransfer.getData(mime);if(raw.length>2048)throw Error('拖入数据无效');const payload=validPayload(JSON.parse(raw));draggingPayload=null;if(tray)storePayload(payload);else dropPayload(payload,target.action);}catch(error){fail(error)}
  },true);
  // In-page dragging follows real pointer travel so cross-stage hover stays continuous.
  // Alt-drag retains native HTML5 MIME transport between tabs; incoming native drops use the same ID checks.
  document.addEventListener('pointerdown',event=>{cancelPointerDrag();if(event.button!==0||event.pointerType==='touch'||event.target.closest?.('input,select,button,a,summary'))return;const payload=dragSource(event);if(payload)pointerDrag={payload:validPayload(payload),pointerId:event.pointerId,x:event.clientX,y:event.clientY,active:false,native:false,hover:null};},true);
  document.addEventListener('pointermove',event=>{
    if(!pointerDrag||pointerDrag.native||event.pointerId!==pointerDrag.pointerId)return;if(event.buttons===0){cancelPointerDrag();return}if(Math.hypot(event.clientX-pointerDrag.x,event.clientY-pointerDrag.y)<10&&!pointerDrag.active)return;
    pointerDrag.active=true;draggingPayload=pointerDrag.payload;event.preventDefault();markTargets(draggingPayload);
    const element=document.elementFromPoint(event.clientX,event.clientY),target=targetAction(element,draggingPayload);
    if(element?.closest('#workspace-panel')){panel.classList.add('dragging');if(collapsed){collapsed=false;applyTraySize()}}else if(target)target.node.classList.add(canTarget(payloadKind(draggingPayload),target.action)?'studio-drop-over':'studio-drop-invalid');
    const stage=element?.closest('[data-stage]')?.dataset.stage;if(stage!==pointerDrag.hover){clearTimeout(hoverTimer);pointerDrag.hover=stage;if(stage)hoverTimer=setTimeout(()=>{if(pointerDrag?.active){setStage(stage);markTargets(pointerDrag.payload)}},450);}
  },{capture:true,passive:false});
  document.addEventListener('pointerup',event=>{const drag=pointerDrag;if(!drag||event.pointerId!==drag.pointerId)return;cancelPointerDrag();if(drag.native||!drag.active)return;event.preventDefault();event.stopPropagation();suppressClickUntil=performance.now()+300;suppressClickPoint={x:event.clientX,y:event.clientY};const element=document.elementFromPoint(event.clientX,event.clientY),target=targetAction(element,drag.payload);if(element?.closest('#workspace-panel'))storePayload(drag.payload);else if(target)dropPayload(drag.payload,target.action);},true);
  document.addEventListener('pointercancel',()=>{if(!pointerDrag?.native){clearHighlights();draggingPayload=null}pointerDrag=null;clearTimeout(hoverTimer)},true);
  window.addEventListener('blur',cancelPointerDrag);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)cancelPointerDrag()});
  document.addEventListener('click',event=>{if(event.detail&&performance.now()<suppressClickUntil&&suppressClickPoint&&Math.hypot(event.clientX-suppressClickPoint.x,event.clientY-suppressClickPoint.y)<12){event.preventDefault();event.stopImmediatePropagation()}},true);
  new MutationObserver(wireDragSources).observe(document.body,{childList:true,subtree:true});wireDragSources();

  $('workspace-toggle').onclick=()=>{if(collapsed)open().catch(fail);else close()};
  $('workspace-from-history').onclick=()=>open().catch(fail);
  $('history-from-workspace').onclick=()=>{$('ws-more').open=false;setHistoryOpen(true)};
  $('ws-close').onclick=()=>{collapsed=!collapsed;applyTraySize()};
  $('ws-size').onchange=()=>{trayHeight=Number($('ws-size').value);collapsed=false;applyTraySize()};
  $('ws-upload-button').onclick=()=>$('ws-upload').click();
  $('ws-resize').onpointerdown=event=>{const start=event.clientY,height=panel.getBoundingClientRect().height;event.preventDefault();$('ws-resize').setPointerCapture(event.pointerId);const move=e=>{trayHeight=Math.max(155,Math.min(Math.floor(innerHeight*.55),height+start-e.clientY));collapsed=false;applyTraySize()};const stop=()=>{$('ws-resize').removeEventListener('pointermove',move);$('ws-resize').removeEventListener('pointerup',stop)};$('ws-resize').addEventListener('pointermove',move);$('ws-resize').addEventListener('pointerup',stop,{once:true})};
  $('ws-resize').onkeydown=event=>{if(['ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();trayHeight=Math.max(155,Math.min(Math.floor(innerHeight*.55),trayHeight+(event.key==='ArrowUp'?25:-25)));collapsed=false;applyTraySize()}};
  $('ws-select').onchange=()=>select($('ws-select').value);
  $('ws-search').oninput=render;
  $('ws-new').onclick=()=>askName('new');$('ws-rename').onclick=()=>askName('rename');
  $('ws-name-cancel').onclick=()=>$('ws-name-dialog').close();
  $('ws-name-form').onsubmit=event=>{event.preventDefault();const name=$('ws-name').value.trim();if(!name)return;const action=nameAction,id=current?.id;$('ws-name-dialog').close();operation('保存工作区名称',async guard=>{const options=json({name});if(action==='rename')options.method='PATCH';const detail=await api(action==='new'?base:`${base}/${id}`,options);guard();accept(detail);toast(action==='new'?'工作区已创建':'名称已更新')})};
  $('ws-confirm-cancel').onclick=()=>$('ws-confirm-dialog').close();
  $('ws-confirm-ok').onclick=()=>{const action=confirmAction;confirmAction=null;$('ws-confirm-dialog').close();if(action)operation('更新工作区',action)};
  $('ws-delete').onclick=()=>{const id=current.id,name=current.name;confirm('删除工作区',`永久删除“${name}”及其中的素材、草稿和保存版本？已创建的生成任务不受影响。`,async guard=>{await api(`${base}/${id}`,{method:'DELETE'});guard();current=null;selectedAsset=null;await refreshList();toast('工作区已删除')})};
  $('ws-upload').onchange=async()=>{const files=[...$('ws-upload').files];$('ws-upload').value='';await upload(files)};
  for(const type of ['dragenter','dragover'])panel.addEventListener(type,event=>{if(![...event.dataTransfer.types].includes('Files'))return;event.preventDefault();panel.classList.add('dragging');if(collapsed){collapsed=false;applyTraySize()}});panel.addEventListener('dragleave',event=>{if(!panel.contains(event.relatedTarget))panel.classList.remove('dragging')});panel.addEventListener('drop',event=>{if(!event.dataTransfer.files.length)return;event.preventDefault();event.stopPropagation();panel.classList.remove('dragging');upload([...event.dataTransfer.files])});
  $('ws-import').onclick=()=>$('ws-import-file').click();
  $('ws-import-file').onchange=()=>{const file=$('ws-import-file').files[0];$('ws-import-file').value='';if(!file)return;operation('导入工作区 ZIP',async guard=>{const fd=new FormData();fd.append('file',file);const detail=await api(`${base}/import`,{method:'POST',body:fd});guard();accept(detail);toast('工作区已导入，点击载入可恢复创作')})};
  $('ws-task-cancel').onclick=hideTask;
  $('ws-task-add').onclick=()=>{if(!current||!pendingTask)return;const id=current.id,{task,assets}=pendingTask,choice=$('ws-task-asset').value,names=choice==='__all'?assets.map(asset=>asset.name):[choice];operation('加入任务素材',async guard=>{let detail;for(const asset_name of names){guard();detail=await api(`${base}/${id}/from-task`,json({kind:task.kind,task_id:task.id,asset_name}));guard();}accept(detail);hideTask();toast(`已加入 ${names.length} 个任务素材`)})};
  $('ws-history').ontoggle=()=>{if($('ws-history').open)loadVersions()};
  $('ws-save').onclick=saveDraft;$('ws-load').onclick=loadDraft;
  $('result-to-workspace').onclick=()=>{const task=studioPreview[activeStage]?.task;if(task)openTask(task)};
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&pointerDrag){pointerDrag=null;draggingPayload=null;clearTimeout(hoverTimer);clearHighlights();event.preventDefault();return;}if(event.key==='Escape'&&!document.querySelector('dialog[open]')){if($('ws-more').open)$('ws-more').open=false;else if(panel.contains(document.activeElement)){event.preventDefault();close();$('ws-close').focus()}}});
  document.addEventListener('click',event=>{if(!$('ws-more').contains(event.target))$('ws-more').open=false});
  function reset(){resetSession();requestEpoch++;reuseEpoch++;if(reuseLoading.open)reuseLoading.close();pointerDrag=null;draggingPayload=null;clearTimeout(hoverTimer);clearHighlights();close();current=null;libraries=[];selectedAsset=null;selectedIds.clear();pendingTask=null;fileOrigins=new WeakMap();hideTask();renderList();render();setFiles($('image-source'),[]);clearFrame('first');clearFrame('last');addReferenceFiles([],true);clearFinishUpload();$('finish-task').value='';updateFinishPreview(null);setFiles($('finish-title-image'),[]);setFiles($('finish-overlay-media'),[]);$('finish-overlay-enabled').checked=false;refreshOverlayControls();}
  window.studioWorkspace={open,close,openTask,rememberTaskFile,reset,refresh:refreshList,renderHistoryInputs,restoreSession,flushSession,scheduleSessionSave};
  addEventListener('resize',applyTraySize);applyTraySize();render();if(accessInfo?.authenticated)refreshList();
})();
