'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const embedded = new URLSearchParams(location.search).has('embedded');
  document.body.classList.toggle('embedded', embedded);
  let config, sessionId, sessionPromise, busy = false, submitting = false;
  let selectedProduct = null, requestId = null;
  const storageKey = 'abv_consultant_session_v3';
  const storage = {get:()=>{try{return sessionStorage.getItem(storageKey);}catch{return null;}},set:value=>{try{value?sessionStorage.setItem(storageKey,value):sessionStorage.removeItem(storageKey);}catch{}}};
  const node = (tag,text,cls) => {const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;};
  const scroll = () => {$('messages').scrollTop=$('messages').scrollHeight;};
  function message(text,role='assistant') {const n=node('div',text,`message ${role}`);$('messages').append(n);scroll();return n;}
  async function session() {
    if(sessionId)return sessionId;
    if(sessionPromise)return sessionPromise;
    sessionPromise=(async()=>{
      sessionId=storage.get();if(sessionId)return sessionId;
      const r=await fetch('/api/session',{method:'POST'});
      if(!r.ok)throw Error('Не вдалося відкрити чат. Спробуйте за хвилину.');
      sessionId=(await r.json()).session_id;storage.set(sessionId);return sessionId;
    })();
    try{return await sessionPromise;}finally{sessionPromise=null;}
  }
  async function post(url,data,retry=true) {
    const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),20000);
    try {
      const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...data,session_id:await session()}),signal:controller.signal});
      if(r.status===403 && r.headers.get('X-Session-Expired') && retry){sessionId=null;storage.set(null);return await post(url,data,false);}
      const result=await r.json().catch(()=>({}));
      if(!r.ok){
        let detail=typeof result.detail==='string'?result.detail:null;
        if(Array.isArray(result.detail)&&result.detail.some(e=>e.loc?.includes('phone')))detail='Перевірте телефон: +380… або 0…';
        throw Error(detail || `Не вдалося виконати запит (${r.status}). Спробуйте ще раз.`);
      }
      return result;
    } catch(e){if(e.name==='AbortError')throw Error('Відповідь затримується. Спробуйте ще раз; номер заявки збережено.');if(e instanceof TypeError)throw Error('Немає зв’язку із сервером. Перевірте інтернет і повторіть спробу.');throw e;}
    finally{clearTimeout(timer);}
  }
  function product(p) {
    const card=node('article',null,'product');card.append(node('h3',p.name));
    if(p.vendor_code)card.append(node('p','Артикул: '+p.vendor_code,'sku'));
    const meta=node('div',null,'meta');meta.append(node('span',p.price===null?'Ціну уточнює менеджер':new Intl.NumberFormat('uk-UA',{style:'currency',currency:'UAH',maximumFractionDigits:2}).format(Number(p.price)),'price'));
    meta.append(node('span',p.available?'В наявності':'Наявність уточнюється',p.available?'stock':'stock out'));card.append(meta);
    const actions=node('div',null,'actions');
    try{const url=new URL(p.url);if(url.protocol==='https:'&&config.product_hosts.includes(url.hostname)){const a=node('a','Картка товару ↗');a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';actions.append(a);}}catch{}
    const button=node('button','Запитати менеджера');button.type='button';button.addEventListener('click',()=>openLead(p));actions.append(button);card.append(actions);$('messages').append(card);scroll();
  }
  function handoff() {const b=node('button','Залишити заявку менеджеру ↗','handoff');b.type='button';b.addEventListener('click',()=>openLead());$('messages').append(b);scroll();}
  async function send(text) {
    if(busy||!text.trim())return;
    busy=true;$('send').disabled=true;$('clear').disabled=true;$('question').value='';
    message(text,'user');const pending=message('Уточнюю за каталогом…');
    try{const data=await post('/api/chat',{message:text});pending.textContent=data.reply;(data.products||[]).forEach(product);if(data.handoff)handoff();}
    catch(e){pending.textContent=e.message;$('question').value=text;}
    finally{busy=false;$('send').disabled=false;$('clear').disabled=false;scroll();$('question').focus();}
  }
  function openLead(p=null) {
    selectedProduct=p;if(!requestId)requestId=crypto.randomUUID();
    $('lead-product').textContent=p?`Питання про: ${p.name}`:'Контакт потрібен лише для опрацювання вашого звернення.';
    $('lead-error').textContent='';$('lead-dialog').showModal();$('name').focus();
  }
  $('composer').addEventListener('submit',e=>{e.preventDefault();send($('question').value.trim());});
  $('question').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send($('question').value.trim());}});
  document.querySelectorAll('[data-question]').forEach(b=>b.addEventListener('click',()=>send(b.dataset.question)));
  $('manager').addEventListener('click',()=>openLead());
  $('cancel').addEventListener('click',()=>{if(!submitting)$('lead-dialog').close();});
  $('lead-dialog').addEventListener('cancel',e=>{if(submitting)e.preventDefault();});
  $('lead-form').addEventListener('submit',async e=>{
    e.preventDefault();if(submitting)return;submitting=true;$('lead-submit').disabled=true;$('cancel').disabled=true;$('lead-error').textContent='';
    const fields=['name','phone','note','consent'];fields.forEach(id=>$(id).disabled=true);
    try {
      const data=await post('/api/manager-request',{request_id:requestId,name:$('name').value.trim(),phone:$('phone').value.trim(),note:$('note').value.trim(),product_id:selectedProduct?.id||null,consent:$('consent').checked});
      $('lead-dialog').close();message(data.message+'\nНомер заявки: '+data.request_id);requestId=null;selectedProduct=null;$('lead-form').reset();
    }catch(e){$('lead-error').textContent=e.message;}
    finally{submitting=false;$('lead-submit').disabled=false;$('cancel').disabled=false;fields.forEach(id=>$(id).disabled=false);}
  });
  $('clear').addEventListener('click',async()=>{
    if(busy)return;$('clear').disabled=true;
    try{await post('/api/clear-history',{});$('messages').replaceChildren();message('Діалог очищено. Що потрібно підібрати?');}
    catch(e){message(e.message);}finally{$('clear').disabled=false;}
  });
  $('close').addEventListener('click',()=>{if(embedded&&document.referrer){try{const parentOrigin=new URL(document.referrer).origin;if(config?.parent_origins.includes(parentOrigin))parent.postMessage({type:'abv:close'},parentOrigin);}catch{}}});
  async function init() {
    try {
      const r=await fetch('/api/config');if(!r.ok)throw Error('Не вдалося завантажити налаштування');config=await r.json();
      $('notice').classList.toggle('demo',config.demo);
      $('notice').textContent=config.demo?'Режим перевірки · заявки лише локальні':config.catalog_ready?'Працюємо за каталогом магазину · контакт залишайте у формі':'Каталог оновлюється · можна залишити заявку менеджеру';
      for(const id of ['privacy','consent-privacy'])$(id).href=config.privacy_url;
      const data=await post('/api/history',{});
      if(data.messages?.length){$('messages').replaceChildren();for(const m of data.messages){message(m.text,m.role==='user'?'user':'assistant');(m.products||[]).forEach(product);if(m.handoff)handoff();}}
    }catch(e){$('notice').textContent=e.message;}
  }
  init();
})();
