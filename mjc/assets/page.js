/* MJC v0.5.0 — 总开关 + 动效升级（spotlight / 弹性缓动 / 数字滚动 / 视图过渡） */
window.onerror=function(msg,src,line){var e=document.getElementById('errbar');if(e){e.style.display='block';e.textContent='❌ 页面脚本错误: '+msg+' (行 '+line+') —— 请把此文字发给小爪';}}
const $=id=>document.getElementById(id);
let STATE=null;
let _sel=null;

function tab(n){
  const apply=()=>{
    ['pg-review','pg-tasks','pg-admin'].forEach(p=>{$(p).style.display='none';});
    const sec=$('pg-'+n);sec.style.display='';
    sec.classList.remove('anim-in');void sec.offsetWidth;sec.classList.add('anim-in');
    ['tb-review','tb-tasks','tb-admin'].forEach(t=>{$(t).className=(t==='tb-'+n)?'on':'';});
    segThumb();
    if(n==='admin')loadState();
    if(n==='tasks'){renderTaskList();renderTaskDetail();}
  };
  if(document.startViewTransition&&!matchMedia('(prefers-reduced-motion: reduce)').matches){
    try{document.startViewTransition(apply);return;}catch(e){}
  }
  apply();
}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
/* ---- 动效工具：spotlight 跟随 / 数字滚动 / 弹性分段 thumb ---- */
let _spCard=null,_spX=0,_spY=0,_spPend=false;
document.addEventListener('pointermove',e=>{
  const c=e.target&&e.target.closest?e.target.closest('.card'):null;if(!c)return;
  _spCard=c;_spX=e.clientX;_spY=e.clientY;
  if(_spPend)return;_spPend=true;
  requestAnimationFrame(()=>{
    _spPend=false;
    if(!_spCard||document.body.classList.contains('lite'))return;
    const r=_spCard.getBoundingClientRect();
    _spCard.style.setProperty('--mx',(_spX-r.left)+'px');
    _spCard.style.setProperty('--my',(_spY-r.top)+'px');
  });
},{passive:true});
function fmtNum(n,dec){n=Number(n)||0;return dec?(Math.round(n*100)/100+''):Math.round(n).toLocaleString('en-US');}
function countUp(el,to,dur,dec){
  if(!el)return;dur=dur||800;to=Number(to)||0;
  const from=Number(el.dataset.v||0)||0;el.dataset.v=to;
  if(from===to){el.textContent=fmtNum(to,dec);return;}
  const t0=performance.now();
  (function f(t){const p=Math.min(1,(t-t0)/dur),e=1-Math.pow(1-p,3);
    el.textContent=fmtNum(from+(to-from)*e,dec);
    if(p<1)requestAnimationFrame(f);})(t0);
}
function segThumb(){
  const seg=document.querySelector('.seg'),th=document.getElementById('seg-thumb'),on=seg&&seg.querySelector('button.on');
  if(!seg||!th||!on)return;
  const sr=seg.getBoundingClientRect(),br=on.getBoundingClientRect();
  th.style.left=(br.left-sr.left)+'px';th.style.width=br.width+'px';
}
window.addEventListener('resize',segThumb);
function toggleLite(){
  const on=document.body.classList.toggle('lite');
  try{localStorage.setItem('mjc_lite',on?'1':'0');}catch(e){}
  const b=document.getElementById('lite-btn');
  if(b)b.textContent=on?'✨ 完整效果':'🐢 轻量模式';
}
function initLite(){
  let pref=null;
  try{pref=localStorage.getItem('mjc_lite');}catch(e){}
  let on=pref==='1';
  if(pref===null){ /* 无人工偏好 → 自动探测软件渲染（VM/无 GPU）→ 默认轻量 */
    try{
      const c=document.createElement('canvas');
      const gl=c.getContext('webgl')||c.getContext('experimental-webgl');
      let r='';
      if(gl){const ext=gl.getExtension('WEBGL_debug_renderer_info');
        r=String((ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER))||'');}
      on=!gl||/llvmpipe|softpipe|swiftshader|virtualbox|svga|software/i.test(r);
    }catch(e){on=false;}
  }
  if(on){
    document.body.classList.add('lite');
    const b=document.getElementById('lite-btn');
    if(b)b.textContent='✨ 完整效果';
  }
}
function tok(){
  let t=localStorage.getItem('mjc_token')||'';
  if(!t){t=prompt('请输入访问 token（私聊小爪获取）:');if(t)localStorage.setItem('mjc_token',t);}
  return t;
}
async function api(path,opts){
  opts=opts||{};opts.headers=Object.assign({'Content-Type':'application/json'},opts.headers||{});
  const t=tok();if(t)opts.headers['X-MJC-Token']=t;
  const r=await fetch(path,opts);const d=await r.json().catch(()=>({}));
  if(r.status===401){localStorage.removeItem('mjc_token');throw new Error('🔒 401 token 错误，请刷新重试');}
  if(d.error)throw new Error(d.error);
  return d;
}
const vBadge=v=>{const c=v==='pass'?'ok':v==='reject'?'bad':v==='revise'?'warn':'idle';return '<span class="badge '+c+'">'+esc((v||'?').toUpperCase())+'</span>';};
function tagBadge(tag){const cls=(tag||'').includes('白菜')?'tag-cabbage':(tag||'').includes('平价')?'tag-mid':(tag||'').includes('旗舰')?'tag-top':'tag-unknown';return '<span class="badge '+cls+'">'+esc(tag||'未知')+'</span>';}

/* ================= 审查台 ================= */
async function run(){
  const btn=$('btn');btn.disabled=true;$('busy').textContent='审查中（请稍候）…';$('out').innerHTML='';
  const body={task:$('task').value,output:$('output').value,screen:$('screen').checked,
    degrade:$('degrade').checked,no_cache:$('nocache').checked};
  try{const d=await api('/api/review',{method:'POST',body:JSON.stringify(body)});render(d);}
  catch(e){$('out').innerHTML='<div class="card">❌ '+esc(e.message)+'</div>';}
  finally{btn.disabled=false;$('busy').textContent='';}
}
function render(d){
  const m=d.meta||{},r=d.record||{},v=r.final||'?';
  const marks=[];
  if(m.cache_hit)marks.push('缓存命中');
  if(m.screened)marks.push(m.screen_passed?'初筛放行':'初筛未过');
  if(m.degraded)marks.push('降级:'+m.degraded+(m.escalated?'→升级':''));
  const pv=r.pass_votes??0, rjv=r.reject_votes??0, rvv=r.revise_votes??0;
  let html='<div class="card"><div class="row">裁决 '+vBadge(v)
    +'<span class="muted">通过 '+pv+' · 拒绝 '+rjv+' · 修订 '+rvv
    +' · 辩论 '+(r.debate_rounds??0)+' 轮 · 耗时 '+(r.elapsed_s??'?')+'s · API '+(m.api_calls??'?')
    +(marks.length?' · '+marks.join(' / '):'')+'</span></div>';
  (r.rounds||[]).forEach(rn=>{
    const kindName=rn.kind==='verifier'?'🔍 验证器':(rn.kind==='screen'?'初筛':(rn.kind==='debate'?'辩论 第 '+rn.round+' 轮':'第 '+rn.round+' 轮 · 独立审查'));
    html+='<div style="margin-top:.7rem"><b class="muted">'+kindName+'</b></div>';
    (rn.opinions||[]).forEach(o=>{
      html+='<div class="jrow"><div class="who">'+esc(o.judge_display||o.judge_id)+'</div><div>'+vBadge(o.verdict)
        +' <span class="muted">conf='+o.confidence+'</span>';
      const its=(o.issues||[]).slice(0,3);
      if(its.length){html+='<div class="iss">'+its.map(i=>'<div>· ['+esc(i.type)+'] '+esc((i.description||'').slice(0,160))+'</div>').join('')+'</div>';}
      if(o.final_reasoning&&!its.length)html+='<div class="muted">'+esc((o.final_reasoning||'').slice(0,180))+'</div>';
      html+='</div></div>';
    });
  });
  html+='</div>';
  $('out').innerHTML=html;
}

/* ================= 后台管理 ================= */
async function loadState(){
  const errbar=$('errbar');
  try{STATE=await api('/api/state');}
  catch(e){errbar.textContent='❌ '+e.message;errbar.style.display='block';return;}
  errbar.style.display='none';
  $('ver').textContent='v'+STATE.version+' · 缓存 '+STATE.cache_entries+' 条'+(STATE.version!=='0.5.0'?' · ⚠️ 页面非最新，请强刷':'');
  const cur=STATE.current||{};
  $('tier-note').textContent='当前档位：'+(cur.tier_label||'');
  $('screen').checked=!!(cur.screen_enabled&&cur.screen_model);
  $('degrade').checked=!!cur.degrade;$('nocache').checked=!cur.cache;
  renderTiers(cur);renderKeys();renderModels(cur);renderStatus(cur);renderSwitch();
}
function renderTiers(cur){
  $('tiers').innerHTML='';
  Object.entries(STATE.tiers||{}).forEach(([id,t])=>{
    const div=document.createElement('div');
    div.className='tier-card'+(cur.tier===id?' sel':'');
    div.innerHTML='<b>'+esc(t.label)+'</b>'+(cur.tier===id?' <span class="badge ok">当前</span>':'')
      +'<div class="muted" style="margin-top:.3rem">'+esc(t.desc)+'</div>'
      +'<div class="tiny" style="margin-top:.4rem">'+esc((t.committee||[]).join(' · '))+'</div>';
    div.onclick=()=>applyCfg({tier:id});
    $('tiers').appendChild(div);
  });
}
function renderKeys(){
  $('keys').innerHTML='';
  (STATE.providers||[]).forEach(p=>{
    const st=p.status;
    let badge='',warn='';
    if(st){
      badge=st.ok?'<span class="badge ok">连通</span>':'<span class="badge bad">探测失败</span> <span class="muted">'+esc(st.at||'')+'</span>';
      if(!st.ok&&st.hint)warn='<div class="muted" style="color:var(--orange);margin-top:.3rem">⚠️ '+esc(st.hint)+'</div>';
    }else badge=p.has_key?'<span class="badge idle">未测试（保存后自动探测）</span>':'<span class="badge warn">未配置 key</span>';
    const card=document.createElement('div');card.className='key-card';
    const hd=document.createElement('div');hd.className='row';
    const nm=document.createElement('b');nm.textContent=p.label;hd.appendChild(nm);
    hd.insertAdjacentHTML('beforeend',badge);card.appendChild(hd);
    const ep=document.createElement('div');ep.className='tiny';ep.style.marginTop='.2rem';
    ep.textContent=p.name+' · '+p.endpoint;card.appendChild(ep);
    const km=document.createElement('div');km.className='tiny';km.textContent='key: '+(p.key_masked||'（无）');card.appendChild(km);
    const row=document.createElement('div');row.className='row';row.style.marginTop='.5rem';
    const inp=document.createElement('input');inp.type='password';inp.id='key-'+p.name;
    inp.placeholder='粘贴新 key（留空=不变）';inp.style.cssText='flex:1;min-width:170px;width:auto';
    const b1=document.createElement('button');b1.className='btn';b1.textContent='保存';b1.onclick=()=>saveKey(p.name);
    const b2=document.createElement('button');b2.className='btn sec';b2.textContent='测试';b2.onclick=()=>probeKey(p.name);
    const b3=document.createElement('button');b3.className='btn danger';b3.textContent='清除';b3.onclick=()=>clearKey(p.name);
    row.append(inp,b1,b2,b3);card.appendChild(row);
    const nt=document.createElement('div');nt.className='muted';nt.style.marginTop='.3rem';nt.textContent=p.note||'';card.appendChild(nt);
    if(warn)card.insertAdjacentHTML('beforeend',warn);
    if(st&&st.tried&&st.tried.length&&!st.ok){
      const tr=document.createElement('div');tr.className='tiny';tr.style.marginTop='.2rem';
      tr.textContent='已尝试: '+st.tried.join(' | ');card.appendChild(tr);}
    $('keys').appendChild(card);
  });
}
function renderModels(cur){
  const com=cur.committee||[];
  const box=$('models');box.innerHTML='';
  const provBad=(STATE.providers||[]).reduce((a,p)=>{a[p.name]=(p.status&&!p.status.ok&&p.status.hint)||null;return a;},{});
  (STATE.catalog||[]).forEach(m=>{
    const inC=com.includes(m.spec);
    const row=document.createElement('label');
    row.className='mrow'+(inC?' sel':'');
    row.title=(m.note||'')+(m.has_key?'':'（该厂商未配 key，先保存）')+(provBad[m.provider]?(' ⚠️'+provBad[m.provider]):'');
    const cb=document.createElement('input');cb.type='checkbox';cb.checked=inC;cb.disabled=!m.has_key;
    const info=document.createElement('span');info.style.cssText='display:flex;align-items:center;gap:.35rem;flex-wrap:wrap';
    info.innerHTML=(m.has_key?'':'<span title="厂商无 key">🚫</span>')
      +(provBad[m.provider]?'<span title="'+esc(provBad[m.provider])+'">⚠️</span>':'')
      +'<b style="font-size:13.5px">'+esc(m.model)+'</b> <span class="tiny">'+esc(m.provider)+'</span> '+tagBadge(m.tag)
      +(inC?' <span class="badge ok">委员</span>':'');
    row.appendChild(cb);row.appendChild(info);
    cb.onchange=()=>{
      const arr=com.filter(s=>s!==m.spec);
      if(cb.checked)arr.push(m.spec);
      if(arr.length<2){cb.checked=!cb.checked;$('cfg-msg').textContent='⚠️ 至少保留 2 个模型';return;}
      applyCfg({committee:arr});
    };
    box.appendChild(row);
  });
  box.insertAdjacentHTML('beforeend','<div class="muted" style="margin-top:.5rem">已选 <b>'+com.length+'</b> 个委员 · 灰显=厂商未配 key</div>');
  const sm=$('screen-model');sm.innerHTML='';
  const opts=['<option value="">（关闭初筛）</option>'].concat((STATE.catalog||[]).filter(m=>m.has_key).map(m=>{
    const s=esc(m.spec);return '<option value="'+s+'"'+(cur.screen_model===m.spec?' selected':'')+'>'+esc(m.model)+' ('+esc(m.provider)+')</option>';}));
  sm.innerHTML=opts.join('');
  $('screen-en').checked=!!cur.screen_enabled;
  $('screen-conf').value=cur.screen_conf??0.75;
  $('cfg-degrade').checked=!!cur.degrade;
  $('cfg-cache').checked=!(cur.cache===false);
}
function renderStatus(){
  $('st-cache').textContent=STATE.cache_entries+' 条（TTL 7 天）';
  $('st-trust').textContent=STATE.trust||'（空）';
  const u=STATE.usage||{};
  countUp($('st-num-reviews'),u.reviews||0);
  countUp($('st-num-tokens'),u.tokens||0);
  countUp($('st-num-cost'),u.cost_yuan||0,900,true);
  countUp($('st-num-nonpass'),u.nonpass||0);
}
/* ---- 自动审查总开关 ---- */
let _swBusy=false;
function renderSwitch(){
  const sw=(STATE&&STATE.auto_switch)||{enabled:true};
  const cb=$('sw-auto');if(!cb)return;
  cb.checked=!!sw.enabled;
  const d=$('sw-desc');
  const when=(sw.updated_at||'').replace('T',' ').slice(0,16);
  d.innerHTML=sw.enabled
    ?'<span class="badge ok">运行中</span> hook 对每条消息自动扫描质检'
      +(when?' <span class="tiny">· 上次变更 '+esc(when)+' by '+esc(sw.by||'?')+'</span>':'')
    :'<span class="badge warn">已暂停</span> 不点火、0 花费、完全静音'
      +(when?' <span class="tiny">· '+esc(when)+' by '+esc(sw.by||'?')+'</span>':'');
}
async function toggleSwitch(){
  if(_swBusy)return;_swBusy=true;
  const cb=$('sw-auto'),want=cb.checked;
  $('sw-desc').textContent=want?'恢复中…':'暂停中…';
  try{
    const r=await api('/api/admin/switch',{method:'POST',body:JSON.stringify({enabled:want})});
    STATE.auto_switch=r.auto_switch;renderSwitch();
  }catch(e){
    cb.checked=!want;$('sw-desc').innerHTML='❌ '+esc(e.message);
  }finally{_swBusy=false;}
}
async function applyCfg(patch){
  $('cfg-msg').textContent='保存中…';
  try{
    const p=patch||{};
    if(!('committee' in p))p.committee=(STATE.current.committee||[]).slice();
    if(!('tier' in p)){
      if($('screen-en').checked&&$('screen-model').value)p.screen_model=$('screen-model').value;else p.screen_model=null;
      p.screen_enabled=$('screen-en').checked;
      if(p.screen_enabled&&!p.screen_model){$('cfg-msg').textContent='❌ 初筛已启用但没选模型';return;}
      p.screen_conf=parseFloat($('screen-conf').value)||0.75;
      p.degrade=$('cfg-degrade').checked;
      p.cache=$('cfg-cache').checked;
    }
    await api('/api/admin/apply',{method:'POST',body:JSON.stringify(p)});
    $('cfg-msg').textContent='✅ 已保存并生效';
    await loadState();
  }catch(e){$('cfg-msg').textContent='❌ '+e.message;}
}
async function saveKey(provider){
  const v=$('key-'+provider).value.trim();
  if(!v){$('cfg-msg').textContent='先粘贴 key';return;}
  $('cfg-msg').textContent='保存 key…';
  try{const d=await api('/api/admin/keys',{method:'POST',body:JSON.stringify({action:'set',provider:provider,key:v})});
    $('key-'+provider).value='';$('cfg-msg').textContent='✅ key 已保存（'+d.masked+'）并自动探测';await loadState();}
  catch(e){$('cfg-msg').textContent='❌ '+e.message;}
}
async function clearKey(provider){
  if(!confirm('清除 '+provider+' 的 key？'))return;
  try{await api('/api/admin/keys',{method:'POST',body:JSON.stringify({action:'clear',provider:provider})});
    $('cfg-msg').textContent='✅ 已清除';await loadState();}
  catch(e){$('cfg-msg').textContent='❌ '+e.message;}
}
let _probeBusy=false;
async function probeKey(provider){
  if(_probeBusy)return;
  _probeBusy=true;$('cfg-msg').textContent='测试 '+provider+'（1 次极小调用）…';
  try{const d=await api('/api/admin/probe',{method:'POST',body:JSON.stringify({provider:provider,force:true})});
    $('cfg-msg').textContent=(d.ok?'✅ ':'❌ ')+d.detail;await loadState();}
  catch(e){$('cfg-msg').textContent='❌ '+e.message;}
  finally{_probeBusy=false;}
}

/* ================= 实时任务（可读分组，任务分离） ================= */
let _liveLast=0,_liveTimer=null,_sessions=[],_fresh=new Set(),_dispCache=null,_pollN=0;
function fmtTime(iso){return esc(String(iso||'').slice(11,19));}
function roleOf(ev){
  if(ev.kind!=='opinion')return '';
  const r=ev.round??1;
  if(r===0)return '初筛';
  if(r===1)return '第 1 轮 · 独立审查';
  return '辩论 第 '+(r-1)+' 轮';
}
function sessionVerdict(evs){
  for(let i=evs.length-1;i>=0;i--){const k=evs[i].kind;if(k==='stage_done'||k==='final')return evs[i].verdict;}
  return null;
}
function sessionLabel(evs){
  const st=evs.find(e=>e.kind==='stage_start');
  if(st)return '阶段闸门 · '+esc(st.stage||'');
  const fn=evs.find(e=>e.kind==='final');
  return fn?'快速审查':'审查';
}
function sessionSha(tid,evs){
  if(/^[0-9a-f]{10}$/.test(tid))return tid;
  const sd=evs.find(e=>e.kind==='stage_done'&&e.sha);
  return sd?sd.sha:null;
}
function buildSessions(){
  _sessions.sort((a,b)=>b.last-a.last);
  _sessions=_sessions.slice(0,14);
  return _sessions;
}
function renderTaskList(){
  const box=$('task-list');if(!box)return;
  buildSessions();
  if(!_sessions.length){box.innerHTML='<div class="muted">暂无任务 —— 审查发生（gate / 审查台 / 自动）后自动出现</div>';return;}
  box.innerHTML='';
  _sessions.forEach(s=>{
    const v=sessionVerdict(s.events);
    const running=v===null;
    const it=document.createElement('div');
    it.className='tsk'+(s.task_id===_sel?' sel':'');
    const label=sessionLabel(s.events);
    it.innerHTML='<div class="t1"><span class="badge '+(running?'idle':(v==='pass'?'ok':'warn'))+'">'+(running?'● 进行中':esc(v.toUpperCase()))+'</span>'
      +'<span>'+label+'</span></div>'
      +'<div class="t2">#'+esc(s.task_id)+' · '+s.events.length+' 事件 · '+fmtTime(s.events[0].at)+'</div>';
    it.onclick=()=>{_sel=s.task_id;renderTaskList();renderTaskDetail();};
    box.appendChild(it);
  });
}
function renderTaskDetail(){
  const box=$('task-detail');if(!box)return;
  const s=_sessions.find(x=>x.task_id===_sel);
  if(!s){box.innerHTML='<div class="empty-hint">👈 选一个任务查看详细过程</div>';return;}
  const evs=s.events;
  const stEv=evs.find(e=>e.kind==='stage_start');
  const v=sessionVerdict(evs);
  let html='<div class="ph">';
  html+='<span class="badge '+(v===null?'idle':(v==='pass'?'ok':'warn'))+'">'+(v===null?'● 进行中':esc(v.toUpperCase()))+'</span>';
  html+='<span class="phn">#'+esc(s.task_id)+'</span>';
  if(stEv)html+='<span class="badge tag-mid">'+esc(stEv.stage||'')+' 检查点</span>';
  const start=evs.find(e=>e.kind==='start');
  if(start){
    const js=(start.judges||[]).map(j=>esc(j.display)).join(' · ');
    html+='<span class="muted">委员会：'+(js||esc((start.pool||[]).join(', ')))+'</span>';
    if(start.screen)html+='<span class="badge idle">初筛开</span>';
    if(start.verifier)html+='<span class="badge idle">验证器开</span>';
  }
  html+='</div>';
  if(stEv)html+='<div class="muted" style="margin-bottom:.4rem">任务：'+esc(String(stEv.task||'').slice(0,140))+'</div>';
  // 阶段化渲染
  let lastRole=null,phaseOpen=false;
  const closePhase=()=>{if(phaseOpen){html+='</div>';phaseOpen=false;}};
  evs.forEach(ev=>{
    const k=ev.kind;
    if(k==='start'||k==='stage_start')return;
    if(k==='verifier_hit'){
      closePhase();html+='<div class="ph" style="margin-top:.7rem"><span class="phn">🔍 确定性验证器</span>'
        +'<span class="badge bad">命中 '+esc(ev.n)+' 处</span></div>';
      (ev.issues||[]).forEach(i=>{html+='<div class="jrow"><div class="who"><span>验证器</span><span class="role">规则验算 · 零成本</span></div><div>['+esc(i.type)+'] '+esc(i.desc)+'</div></div>';});
      return;
    }
    if(k==='opinion'){
      const role=roleOf(ev);
      if(role!==lastRole){closePhase();lastRole=role;
        html+='<div class="ph" style="margin-top:.8rem"><span class="phn">'+(role==='初筛'?'🧪 '+role:role==='第 1 轮 · 独立审查'?'🗳️ '+role:'⚔️ '+role)+'</span></div>'
          +'<div id="ph-'+esc(String(role))+'">';phaseOpen=true;}
      const fresh=_fresh.has(ev.ts_ms)?' fresh':'';
      html+='<div class="jrow'+fresh+'"><div class="who"><span>🦉 '+esc(ev.display||ev.judge)+'</span>'
        +'<span class="role">'+fmtTime(ev.at)+' · conf='+esc(ev.confidence??'?')+'</span></div><div>'+vBadge(ev.verdict)
        +(ev.n_issues?' <span class="badge warn">'+ev.n_issues+' 条意见</span>':'')
        +(ev.verdict==='pass'&&!ev.n_issues?'<span class="muted">未发现问题</span>':'')
        +(ev.issues&&ev.issues.length?'<div class="iss">'+ev.issues.map(i=>'<div>· ['+esc(i.type)+'] '+esc(i.desc)+'</div>').join('')+'</div>':'')
        +'</div></div>';
      return;
    }
    if(k==='final'){
      closePhase();lastRole=null;
      html+='<div class="ph" style="margin-top:.9rem"><span class="phn">🏁 最终裁决</span>'+vBadge(ev.verdict)
        +'<span class="muted">辩论 '+esc(ev.debate_rounds??0)+' 轮 · API '+esc(ev.api_calls??'?')+' 次'
        +(ev.tokens?' · tokens '+esc(ev.tokens)+' ≈¥'+esc(ev.cost_yuan??''):'')+'</span></div>';
      return;
    }
    if(k==='stage_done'){
      closePhase();
      html+='<div class="ph" style="margin-top:.6rem"><span class="phn">✅ 闸门 '+esc(ev.stage||'')+'</span>'+vBadge(ev.verdict)
        +'<span class="muted">'+fmtTime(ev.at)+' · '+(ev.verdict==='pass'?'放行进入下一阶段':'阻断，修复后重跑本闸门')+'</span></div>';
      return;
    }
  });
  closePhase();
  // 主 Agent 处置（dispositions join by sha）
  const sha=sessionSha(s.task_id,evs);
  const disps=(_dispCache||[]).filter(d=>sha&&d.review&&d.review.sha===sha);
  if(disps.length){
    html+='<div class="ph" style="margin-top:.9rem"><span class="phn">🦞 主 Agent 处置</span><span class="muted">'+disps.length+' 条意见逐条答复</span></div>';
    disps.forEach(d=>{
      (d.decisions||[]).forEach(dc=>{
        const iss=(d.review.issues||[])[dc.idx]||{};
        const ok=dc.adopted;
        html+='<div class="jrow"><div class="who"><span>'+(ok?'✅ 采纳':'❌ 未采纳')+'</span>'
          +'<span class="role">'+esc(dc.action||'')+'</span></div><div>'
          +'<span class="iss"><b>['+esc(iss.type||'')+']</b> '+esc((iss.desc||'').slice(0,120))+'</span>'
          +(dc.note?'<div class="iss muted">理由：'+esc(dc.note)+'</div>':'')
          +'</div></div>';
      });
    });
  }
  html+='<div class="muted" style="margin-top:.9rem">事件流 · logs/auto/live.jsonl · 自动每 2 秒刷新</div>';
  box.innerHTML=html;
  const card=$('task-detail-card');if(card)card.scrollTop=card.scrollHeight;
}
function selectOrKeep(){
  if(!_sel&&_sessions.length)_sel=_sessions[0].task_id;
}
async function pollLive(){
  _pollN++;
  try{
    const d=await api('/api/live?after='+_liveLast);
    if(d.events&&d.events.length){
      d.events.forEach(ev=>{
        const tid=ev.task_id||'?';
        let s=_sessions.find(x=>x.task_id===tid);
        if(!s){s={task_id:tid,events:[],last:0};_sessions.push(s);}
        s.events.push(ev);s.last=Math.max(s.last,ev.ts_ms||0);
        _fresh.add(ev.ts_ms);
        if(s.events.length>300)s.events=s.events.slice(-300);
      });
      _liveLast=d.last||_liveLast;
      selectOrKeep();
      if($('pg-tasks').style.display!=='none'){renderTaskList();renderTaskDetail();}
      else if(_sessions.some(s=>sessionVerdict(s.events)===null)){$('live-dot').classList.add('on');}
      // 新任务到达且没选中 → 自动选中最新
      if(_sessions.length&&!d.events.every(e=>e.task_id!==_sessions[0].task_id)){}
    }
    if(_pollN%8===1){ // ~16s 刷新一次处置缓存（含版本号校验位）
      try{const st=await api('/api/state');_dispCache=(st&&st.dispositions)||[];}
      catch(e){}
    }
    // 清 fresh 标记（2 轮后）
    if(_pollN%2===0){_fresh.clear();}
  }catch(e){/* 静默重试 */}
}
function liveLoop(){
  if(_liveTimer)clearInterval(_liveTimer);
  pollLive();
  _liveTimer=setInterval(pollLive,2000);
}
liveLoop();
loadState();
initLite();
segThumb();
setTimeout(segThumb,350);
(function(){const el=document.getElementById('sw-auto');if(el)el.addEventListener('change',toggleSwitch);})();
