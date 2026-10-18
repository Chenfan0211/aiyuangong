const token=localStorage.getItem('store-agent-token')||'';
  const $=id=>document.getElementById(id);
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function notice(text,isError){const el=$('notice');if(!text){el.hidden=true;return}el.hidden=false;el.textContent=text;el.style.background=isError?'#fee4e2':'#fff1cc';el.style.color=isError?'#b42318':'#93370d'}
  async function api(path,options){const opts=options||{};opts.headers=opts.headers||{};if(token)opts.headers['Authorization']='Bearer '+token;const r=await fetch(path,opts);const data=await r.json().catch(()=>({}));if(!r.ok)throw new Error(data.error||data.detail||('HTTP '+r.status));return data}
  async function post(path,body){return api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
  const fmtTime=v=>esc(v).slice(0,19).replace('T',' ');
  const tagOf=s=>`<span class="tag ${esc(s)}">${esc(s)}</span>`;

  async function loadQueue(){
    try{
      const data=await api('/api/approvals?status='+encodeURIComponent($('statusFilter').value));
      $('who').textContent=(data.store_id||'')+' · '+(data.approvals.length)+' 条';
      $('counts').textContent=Object.entries(data.count_by_status||{}).map(([k,v])=>k+' '+v).join(' / ')||'无审批单';
      const box=$('queue');box.innerHTML='';
      if(!(data.approvals||[]).length){box.innerHTML='<div class="empty">该状态下没有审批单。</div>';return}
      data.approvals.forEach(a=>{
        const card=document.createElement('div');card.className='card';
        const payload=JSON.stringify(a.payload_snapshot||{});
        card.innerHTML=`<div class="t">${esc((a.payload_snapshot||{}).title||a.action_type)}</div>
          <div>${tagOf(a.status)}<span class="tag">${esc(a.action_type)}</span></div>
          <div class="s">申请人 ${esc(a.requested_by)} · ${fmtTime(a.requested_at)}${a.task_id?' · 任务 '+esc(a.task_id):''}</div>
          <div class="s mono">${esc(payload).slice(0,160)}</div>`;
        if(a.status==='pending'){
          const acts=document.createElement('div');acts.className='acts';
          const ok=document.createElement('button');ok.className='ok';ok.textContent='通过';ok.onclick=()=>decide(a,'approved');
          const no=document.createElement('button');no.className='no';no.textContent='驳回';no.onclick=()=>decide(a,'rejected');
          acts.append(ok,no);card.appendChild(acts);
        }
        box.appendChild(card);
      });
    }catch(error){notice('审批队列读取失败：'+error.message,true);$('queue').innerHTML='<div class="empty">读取失败</div>'}
  }
  async function decide(approval,decision){
    let reason='';
    if(decision==='rejected'){reason=window.prompt('驳回原因（必填）：')||'';if(!reason){notice('驳回必须填写原因',true);return}}
    else{reason=window.prompt('通过备注（可留空）：')||''}
    try{await post('/api/approvals/'+encodeURIComponent(approval.approval_id)+'/decide',{decision,reason});notice('已'+(decision==='approved'?'通过':'驳回')+'：'+(approval.payload_snapshot&&approval.payload_snapshot.title||approval.action_type),false);loadQueue();loadAudit()}
    catch(error){notice('审批失败：'+error.message,true)}
  }
  async function loadAudit(){
    try{
      const params=new URLSearchParams({limit:'50'});
      if($('eventType').value)params.set('event_type',$('eventType').value);
      if($('actorFilter').value.trim())params.set('actor_id',$('actorFilter').value.trim());
      const data=await api('/api/audit?'+params.toString());
      const s=data.summary||{};
      $('auditSummary').textContent=`近 ${s.days} 天 ${s.total} 条 · 越权 ${s.permission_denied} · 登录失败 ${s.login_failed}`;
      const select=$('eventType');
      if(select.options.length<=1){(data.event_types||[]).forEach(t=>{const o=document.createElement('option');o.value=t;o.textContent=t;select.appendChild(o)})}
      const body=$('auditBody');body.innerHTML='';
      if(!(data.events||[]).length){body.innerHTML='<tr><td colspan="5" class="empty">没有匹配的审计事件。</td></tr>';return}
      data.events.forEach(e=>{
        const tr=document.createElement('tr');
        tr.innerHTML=`<td class="mono">${fmtTime(e.created_at)}</td><td>${tagOf(e.event_type)}</td>
          <td class="mono">${esc(e.actor_id)}</td><td class="mono">${esc(e.object_type)}#${esc(e.object_id).slice(0,40)}</td>
          <td class="mono">${esc(JSON.stringify(e.after||e.before||{})).slice(0,110)}</td>`;
        body.appendChild(tr);
      });
    }catch(error){notice('审计读取失败：'+error.message,true);$('auditBody').innerHTML='<tr><td colspan="5" class="empty">读取失败（可能缺少 audit:read 权限）</td></tr>'}
  }
  async function loadToolRuns(){
    try{
      const data=await api('/api/tool-runs?limit=30');
      const body=$('toolBody');body.innerHTML='';
      if(!(data.runs||[]).length){body.innerHTML='<tr><td colspan="6" class="empty">暂无工具调用记录。</td></tr>';return}
      data.runs.forEach(r=>{
        const tr=document.createElement('tr');
        tr.innerHTML=`<td class="mono">${fmtTime(r.created_at)}</td><td>${esc(r.tool_name)}</td>
          <td class="mono">${esc(r.actor_id)}</td><td class="mono">${esc(r.session_id||'-')}</td>
          <td>${tagOf(r.status)}</td><td class="mono">${esc(JSON.stringify(r.input_redacted||{})).slice(0,80)}</td>`;
        body.appendChild(tr);
      });
    }catch(error){$('toolBody').innerHTML='<tr><td colspan="6" class="empty">读取失败（需要 audit:read 权限）</td></tr>'}
  }
  $('refresh').onclick=loadQueue;$('refreshAudit').onclick=loadAudit;
  $('statusFilter').onchange=loadQueue;$('eventType').onchange=loadAudit;
  if(!token){notice('未登录：请先在对话页面登录，再回到本页。',true)}
  else{notice('');loadQueue();loadAudit();loadToolRuns()}
