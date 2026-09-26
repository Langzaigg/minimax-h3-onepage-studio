/* CPU-only UI payload contracts. Run with node --test tests/frontend_postprocess.test.cjs. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'templates/video_generate.html'), 'utf8');
function functionSource(name) {
  const begin = html.indexOf(`  function ${name}(`);
  assert.ok(begin >= 0, `Missing function ${name}`);
  const end = html.indexOf('\n  }', begin);
  assert.ok(end > begin);
  return html.slice(begin, end + 4);
}
function load(name) {
  const context = vm.createContext({});
  vm.runInContext(functionSource(name), context);
  return (...args) => JSON.parse(JSON.stringify(context[name](...args)));
}
const quick = load('quickResampleParams');
const finish = load('finishRequestParameters');
test('inline application scripts and material library parse', () => {
  for (const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(script[1]);
  new vm.Script(fs.readFileSync(path.join(root, 'static/js/studio-workspaces.js'), 'utf8'));
});
test('one-click preserves exact seed and original delivery without forced FPS', () => {
  const result = quick({id:'a'.repeat(32), status:'completed', assets:{video:'/owned/video'},
    seed_text:'9223372036854775807', target_resolution:'1440p', prompt:'original prompt', duration:8},
    'redgraft_msr', '720p');
  assert.equal(result.seed, '9223372036854775807');
  assert.equal(result.target_resolution, '1440p');
  assert.equal(result.prompt, 'original prompt');
  assert.equal(result.interpolation, 'none');
  assert.equal(result.loop, 'false');
  assert.equal('target_fps' in result, false);
});
test('LTX doubles cadence only on explicit selection', () => {
  const values = {preset:'redgraft_msr', mediaKind:'video', targetResolution:'1080p', seed:'9223372036854775807'};
  assert.equal(finish(values).interpolation, 'none');
  assert.equal(finish({...values, interpolation:'2x'}).interpolation, '2x');
  assert.equal('target_fps' in finish(values), false);
  assert.equal(finish(values).seed, values.seed);
  assert.throws(() => finish({...values, mediaKind:'image'}));
});
test('DLSS cannot leak resize, interpolation or old loop parameters', () => {
  const result = finish({preset:'dlss_nr', mediaKind:'video', controlsVersion:1, look:'detail', strength:.35,
    previewTime:1.25, targetResolution:'720p', interpolation:'2x', loop:true, seed:'99'}, true);
  assert.deepEqual(result, {preset:'dlss_nr', dlss_controls_version:'1', dlss_look:'detail', dlss_strength:'0.35',
    preview:'true', preview_time:'1.25'});
  const image = finish({preset:'dlss_nr', mediaKind:'image'}, true);
  assert.equal('preview_time' in image, false);
  assert.equal('target_resolution' in image, false);
});
test('four native DLSS controls preserve values independently from custom enable switches', () => {
  const result=finish({preset:'dlss_nr',mediaKind:'video',style:'cinematic',parameters:{local_tone:.7,skin_mask:.2},enabled:{local_tone:false},previewTime:2},true);
  assert.equal(result.dlss_controls_version,'2');assert.equal(result.dlss_style,'cinematic');
  assert.equal(result.dlss_local_tone,'0.7');assert.equal(result.dlss_local_tone_enabled,'false');
  assert.equal(result.dlss_output_blend,'1');assert.equal(result.dlss_local_structure,'0.5');
  assert.equal(result.dlss_skin_mask,'0.2');assert.equal(result.dlss_skin_mask_enabled,'true');
  assert.equal('dlss_style_strength' in result,false);assert.equal('target_fps' in result,false);
  assert.equal('target_resolution' in result,false);assert.equal('loop' in result,false);
});
test('DLSS experimental range is explicit and bounded at five', () => {
  assert.throws(()=>finish({preset:'dlss_nr',parameters:{local_tone:2}}));
  const result=finish({preset:'dlss_nr',experimental:true,parameters:{local_tone:5,output_blend:3}});
  assert.equal(result.dlss_local_tone,'5');assert.equal(result.dlss_output_blend,'3');
  assert.equal(result.dlss_allow_experimental,'true');
  assert.throws(()=>finish({preset:'dlss_nr',experimental:true,parameters:{local_tone:5.01}}));
});
test('side-by-side preview compares selected source frame and result without changing output assets', () => {
  const pair=load('finishComparisonAssets'),task={preset:'dlss_nr',assets:{source_video:'/source.mp4',preview_source:'/selected.png',image:'/result.png'}};
  assert.deepEqual(pair(task),{kind:'image',original:'/selected.png',result:'/result.png'});
  assert.equal(task.assets.image,'/result.png');
  assert.deepEqual(pair({preset:'dlss_nr',assets:{source_video:'/source.mp4',video:'/result.mp4'}}),{kind:'video',original:'/source.mp4',result:'/result.mp4'});
  assert.equal(pair({preset:'redgraft_msr',assets:task.assets}),null);
});
test('workspace snapshot retains disabled native values, version and experimental selection', () => {
  const source=fs.readFileSync(path.join(root,'static/js/studio-workspaces.js'),'utf8');
  const nodes={};
  const context=vm.createContext({$:id=>nodes[id]||(nodes[id]={type:'text',value:'',files:[]}),activeStage:'finish',mode:'hybrid',referenceFileBuffer:[],finishWorkspaceSource:null,finishStudioSource:null,studioPreview:{},uiSnapshot:()=>({drawerOpen:true}),activeVideoPreviewTask:null,studioPendingIds:new Set(),comparisonOrigin:()=>null});
  vm.runInContext(source.split('\n').find(line=>line.trimStart().startsWith('const fields =')),context);
  const start=source.indexOf('  function snapshot()'),end=source.indexOf('  function comparisonOrigin(',start);
  vm.runInContext(source.slice(start,end),context);
  nodes['finish-dlss-controls-version']={type:'hidden',value:'2'};
  nodes['finish-dlss-local-tone']={type:'range',value:'2.5',disabled:true};
  nodes['finish-dlss-local-tone-enabled']={type:'checkbox',checked:false};
  nodes['finish-dlss-experimental']={type:'checkbox',checked:true};
  nodes['finish-preview-mode']={type:'hidden',value:'side-by-side'};
  const fields=context.snapshot().fields;
  assert.equal(fields['finish-dlss-controls-version'],'2');
  assert.equal(fields['finish-dlss-local-tone'],'2.5');
  assert.equal(fields['finish-dlss-local-tone-enabled'],false);
  assert.equal(fields['finish-dlss-experimental'],true);
  assert.equal(fields['finish-preview-mode'],'side-by-side');
});
test('dragging the source side of a comparison preserves the original asset identity', () => {
  const source=fs.readFileSync(path.join(root,'static/js/studio-workspaces.js'),'utf8');
  const start=source.indexOf('  function dragSource('),end=source.indexOf('  function wireDragSources(',start);
  const context=vm.createContext({activeStage:'finish',current:null,studioPreview:{finish:{type:'image',task:{id:'task',kind:'postprocess',assets:{preview_source:'/frame.png',image:'/result.png'}}}}});
  vm.runInContext(source.slice(start,end),context);
  for(const [side,expected]of [['source','preview_source'],['result','image']]){
    const event={target:{closest:selector=>selector==='[data-comparison-side]'?{dataset:{comparisonSide:side}}:null}};
    assert.equal(context.dragSource(event).asset_name,expected);
  }
});
test('late restoration from the previous identity cannot overwrite the new identity', async () => {
  const source=fs.readFileSync(path.join(root,'static/js/studio-workspaces.js'),'utf8');
  const start=source.indexOf('  async function restoreSession()'),end=source.indexOf('  function resetSession()',start);
  let owner='A',completeA,applied=0;
  const context=vm.createContext({sessionOwner:()=>owner,studioCapabilities:{},sessionLoadedOwner:'',sessionRestoring:false,sessionRestoreEpoch:0,sessionBaseline:{fields:{}},
    readLocalSession:key=>key==='A'?new Promise(resolve=>{completeA=resolve}):Promise.resolve({record:null,media:new Map()}),readSessionDraft:()=>null,
    applyFormFields(){applied++},toast(){throw Error('Cancelled restoration should not display an error')},
  });
  vm.runInContext(source.slice(start,end),context);
  const pendingA=context.restoreSession();owner='B';context.sessionRestoreEpoch++;context.sessionRestoring=false;
  await context.restoreSession();completeA({record:{version:1,snapshot:{fields:{prompt:'private A'}}},media:new Map()});await pendingA;
  assert.equal(context.sessionLoadedOwner,'B');assert.equal(context.sessionRestoring,false);assert.equal(applied,0);
});
test('retired methods cannot be submitted through the shared form builder', () => {
  for (const preset of ['anime_sr','h3_refine','ltx25_msr','redgraft_pixel_2stage']) {
    assert.throws(() => finish({preset, mediaKind:'video'}));
  }
});
test('delivery selection does not change manual resolution, aspect or Turbo', () => {
  const fields = {
    'megapixels':{value:'0.9',options:[{value:'0.5'},{value:'0.9'}]},
    'aspect':{value:'9:16'}, 'hybrid-turbo':{value:'fast',options:[{value:'off'},{value:'fast'}]},
    'video-auto-postprocess':{checked:false}, 'source-resolution-profile':{value:''},
    'target-resolution':{value:'720p'},'video-target':{},'finish-target':{},
  };
  const context = vm.createContext({$:id=>fields[id], refreshDeliveryInfo(){},
    addProfileResolutionOptions(){},syncHybridTurboUi(){},
    restoreSourceAspect(value){fields.aspect.value=value},
  });
  vm.runInContext("let mode='hybrid',deliverySourceCustomized=false,lastManualSource=null;const manualSourceByMode=new Map();const deliveryTargets=[{id:'720p'},{id:'1080p'}];const deliveryProfiles=[{id:'1080p',source_megapixels:.5}];function deliveryTarget(){return{id:$('target-resolution').value}}", context);
  for(const name of ['rememberManualSource','restoreManualSource','applyDeliveryRecommendation','setDeliveryTarget']) {
    const source=html.split('\n').find(line=>line.trimStart().startsWith(`function ${name}(`));
    vm.runInContext(source,context);
  }
  context.setDeliveryTarget('1080p',true);
  assert.equal(fields.megapixels.value,'0.9');
  assert.equal(fields.aspect.value,'9:16');
  assert.equal(fields['hybrid-turbo'].value,'fast');
  context.rememberManualSource();fields['video-auto-postprocess'].checked=true;context.applyDeliveryRecommendation();
  assert.equal(fields.megapixels.value,'0.5');assert.equal(fields.aspect.value,'16:9');
  assert.equal(fields['source-resolution-profile'].value,'1080p');
  fields['video-auto-postprocess'].checked=false;context.restoreManualSource();
  assert.equal(fields.megapixels.value,'0.9');assert.equal(fields.aspect.value,'9:16');
  assert.equal(fields['hybrid-turbo'].value,'fast');assert.equal(fields['source-resolution-profile'].value,'');
});
test('video form explicitly opts out of source profiles when auto processing is off', () => {
  const node=value=>({value,files:[],checked:false});
  const nodes={prompt:node('user prompt'),duration:node('8'),megapixels:node('0.9'),aspect:node('9:16'),
    'hybrid-preset':node('anime'),'hybrid-turbo':node('off'),'target-resolution':node('1080p'),
    'source-resolution-profile':node('1080p'),'video-auto-postprocess':node(''),
    'polish-before-generate':node(''),seed:node('9223372036854775807'),
    'first-frame':node(''),'last-frame':node(''),'reference-files':node('')};
  const context=vm.createContext({$:id=>nodes[id],FormData,mode:'hybrid',nsfwOn:()=>false,
    groupedReferenceFiles:()=>({image:[],video:[],audio:[]})});
  vm.runInContext(html.split('\n').find(line=>line.trimStart().startsWith('function formData(')),context);
  const manual=context.formData();
  assert.equal(manual.get('auto_postprocess'),'false');assert.equal(manual.get('source_resolution_profile'),'');
  assert.equal(manual.get('resolution_megapixels'),'0.9');assert.equal(manual.get('aspect_ratio'),'9:16');
  assert.equal(manual.get('seed'),'9223372036854775807');
  nodes['video-auto-postprocess'].checked=true;
  assert.equal(context.formData().get('auto_postprocess'),'true');
  assert.equal(context.formData().get('source_resolution_profile'),'1080p');
});
test('authenticated browser session restores without saved credentials or login modal', async () => {
  const session={authenticated:true,role:'admin',name:'Administrator'};
  let loaded=0, updated;
  const context=vm.createContext({loadConfig:async()=>{},accessMode:'register',accessStorageKey:'unused',
    fetch:async(url,options)=>{assert.equal(url,'/api/access/status');assert.equal(options.cache,'no-store');return{ok:true,json:async()=>session}},
    updateAuth:value=>{updated=value},load:async()=>{loaded++},
    localStorage:{getItem(){throw Error('Existing session must not read saved credentials')}},
    showAuth(){throw Error('Existing session must not open login')},
  });
  vm.runInContext(html.split('\n').find(line=>line.trimStart().startsWith('async function initialize(')),context);
  await context.initialize();assert.equal(updated,session);assert.equal(loaded,1);
});
test('only the current two-route server contract enables new video submissions', () => {
  const compatible=load('supportsCurrentStudioContract');
  const current={postprocess:{default_preset:'redgraft_msr',presets:[{id:'dlss_nr',available:false},{id:'redgraft_msr'},{id:'h3_refine',archived:true}]}};
  assert.equal(compatible(current),true); // Missing local models are separate from protocol compatibility.
  assert.equal(compatible({postprocess:{...current.postprocess,default_preset:'anime_sr'}}),false);
  assert.equal(compatible({postprocess:{...current.postprocess,presets:[...current.postprocess.presets,{id:'ltx25_msr'}]}}),false);
  assert.equal(compatible({postprocess:{presets:[{id:'redgraft_msr'},{id:'dlss_nr'}]}}),false);
  assert.equal(compatible(null),false);
});
