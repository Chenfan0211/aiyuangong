const token=localStorage.getItem('store-agent-token')||'';
  const $=id=>document.getElementById(id);
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=v=>v===null||v===undefined?'缺数':Number(v).toLocaleString('zh-CN');
  const pct=v=>v===null||v===undefined?'缺数':v+'%';
  function notice(text,isError){const el=$('notice');if(!text){el.hidden=true;return}el.hidden=false;el.textContent=text;el.style.background=isError?'#fee4e2':'#fff1cc';el.style.color=isError?'#b42318':'#93370d'}
  async function api(path){const r=await fetch(path,{headers:token?{Authorization:'Bearer '+token}:{}});const data=await r.json().catch(()=>({}));if(!r.ok)throw new Error(data.error||data.detail||('HTTP '+r.status));return data}
  const cls=v=>v===null?'flat':(v>0?'up':(v<0?'down':'flat'));
  const arrow=v=>v===null?'缺对比':(v>0?'▲ ':'▼ ')+v+'%';
  const tag=s=>`<span class="tag ${esc(s)}">${esc(s)}</span>`;

  function renderKpis(data){
    const k=data.kpis,c=data.change_percent;
    const cards=[
      ['曝光',num(k.impressions),c.impressions],
      ['详情访问',num(k.detail_views),c.detail_views],
      ['支付订单',num(k.paid_orders),c.paid_orders],
      ['核销订单',num(k.verified_orders),c.verified_orders],
      ['访问率',pct(k.view_rate),null],['支付转化率',pct(k.pay_rate),null],
      ['核销率',pct(k.verify_rate),null]
    ];
    if(data.financial_included){cards.splice(4,0,['GMV','¥'+num(k.gmv),c.gmv]);cards.push(['退款率',pct(k.refund_rate),null])}
    $('kpis').innerHTML=cards.map(([label,value,chg])=>`<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div>`
      +(chg===null||chg===undefined?'':`<div class="${cls(chg)}">${arrow(chg)}</div>`)+`</div>`).join('');
  }
  function renderTrend(data){
    const rows=data.trend||[];
    if(!rows.length){$('trend').innerHTML='<div class="empty">该区间没有数据。</div>';return}
    const max=Math.max(...rows.map(r=>r.impressions||0))||1;
    $('trend').innerHTML=rows.map(r=>`<div class="bar" title="${r.date}｜曝光 ${num(r.impressions)}｜支付 ${num(r.paid_orders)}">
      <i style="height:${Math.max(2,Math.round((r.impressions||0)/max*100))}%"></i><b>${esc(r.date).slice(5)}</b></div>`).join('');
  }
  function renderFunnel(data){
    const k=data.kpis;
    const steps=[['曝光',k.impressions,'—'],['详情访问',k.detail_views,pct(k.view_rate)],['支付订单',k.paid_orders,pct(k.pay_rate)],['核销订单',k.verified_orders,pct(k.verify_rate)]];
    $('funnel').innerHTML=steps.map(([label,value,rate])=>`<tr><th style="width:130px">${label}</th><td>${num(value)}</td><td class="mono">${rate}</td></tr>`).join('');
  }
  function renderPackages(data){
    const rows=data.packages||[];
    if(!rows.length){$('packages').innerHTML='<tr><td colspan="6" class="empty">该区间没有套餐明细（先导入套餐报表）。</td></tr>';return}
    $('packages').innerHTML=rows.map(p=>`<tr><td>${esc(p.name)}</td><td>${esc(p.platform)}</td><td>${num(p.detail_views)}</td>
      <td>${num(p.paid_orders)}</td><td>${pct(p.conversion_rate)}</td><td>${p.gross_margin_rate===null?'缺成本':(p.gross_margin_rate*100).toFixed(1)+'%'}</td></tr>`).join('');
  }
  function renderSide(data){
    const q=data.data_quality;
    $('quality').innerHTML=`<tr><th>窗口</th><td>${esc(data.window.start)} ~ ${esc(data.window.end)}</td></tr>
      <tr><th>新鲜度</th><td>${Object.entries(q.freshness).map(([k,v])=>esc(k)+' '+tag(v)).join(' ')}</td></tr>
      <tr><th>缺日</th><td>${q.missing_day_count} 天</td></tr><tr><th>跳变</th><td>${q.anomaly_count} 处</td></tr>`;
    const t=data.tasks, a=data.approvals||{};
    $('tasks').innerHTML=`<tr><th>任务</th><td>${t.total} 项（未关闭 ${t.open_count}／逾期 ${t.overdue_count}）</td></tr>
      <tr><th>完成率</th><td>${pct(t.completion_rate)}</td></tr>
      <tr><th>待审批</th><td>${a.pending||0} 张</td></tr>`;
    $('issues').innerHTML=(data.issues||[]).length?data.issues.map(i=>`<li>${esc(i)}</li>`).join(''):'<li class="empty">未发现异常</li>';
  }
  async function load(){
    try{
      const data=await api('/api/dashboard?days='+encodeURIComponent($('days').value));
      renderKpis(data);renderTrend(data);renderFunnel(data);renderPackages(data);renderSide(data);
      notice(data.has_data?'':'该区间没有已校验的平台数据：请先导出并导入官方报表。',!data.has_data);
    }catch(error){notice('看板加载失败：'+error.message,true);$('kpis').innerHTML='<div class="empty">加载失败</div>'}
  }
  async function report(kind){
    try{const data=await api('/api/reports/'+kind);$('report').textContent=data.report;notice('已生成'+data.title,false)}
    catch(error){notice('生成失败：'+error.message,true)}
  }
  $('days').onchange=load;$('weekly').onclick=()=>report('weekly');$('monthly').onclick=()=>report('monthly');
  if(!token){notice('未登录：请先在对话页面登录，再回到本页。',true)}else{notice('');load()}
