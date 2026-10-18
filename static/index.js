const prefix='store-operations-system-v1:';
  const savedEls=[...document.querySelectorAll('[data-save]')];
  const checks=[...document.querySelectorAll('[data-check]')];
  function load(){
    savedEls.forEach(el=>{const v=localStorage.getItem(prefix+el.dataset.save);if(v!==null)el.value=v});
    checks.forEach(el=>{el.checked=localStorage.getItem(prefix+'check:'+el.dataset.check)==='1'});
    updateProgress();
  }
  function saveNow(){
    savedEls.forEach(el=>localStorage.setItem(prefix+el.dataset.save,el.value));
    checks.forEach(el=>localStorage.setItem(prefix+'check:'+el.dataset.check,el.checked?'1':'0'));
    updateProgress();const btn=document.querySelector('.btn.primary');const old=btn.textContent;btn.textContent='已保存';setTimeout(()=>btn.textContent=old,1200);
  }
  function updateProgress(){
    checks.forEach(el=>el.closest('.check').classList.toggle('done',el.checked));
    const n=checks.filter(el=>el.checked).length;document.getElementById('progressText').textContent=`${n} / ${checks.length}`;document.getElementById('progressBar').style.width=(checks.length?n/checks.length*100:0)+'%';
  }
  savedEls.forEach(el=>el.addEventListener('change',saveNow));checks.forEach(el=>el.addEventListener('change',saveNow));load();
