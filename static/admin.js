'use strict';
(() => {
const $=id=>document.getElementById(id);let token='',offset=0,total=0,timer;
const states={new:'Нова',in_progress:'У роботі',done:'Опрацьована'};
const notices={pending:'Очікує сповіщення',sending:'Надсилається',sent:'Надіслано в Telegram',needs_review:'Потрібна перевірка доставки'};
function idle(){clearTimeout(timer);if(token)timer=setTimeout(logout,15*60*1000);}
function logout(){token='';$('token').value='';$('workspace').hidden=true;$('login').hidden=false;$('leads').replaceChildren();$('overview').textContent='';$('error').textContent='';clearTimeout(timer);}
const n=(tag,text)=>{const e=document.createElement(tag);if(text)e.textContent=text;return e;};
async function api(path,options={}){idle();const r=await fetch(path,{...options,headers:{'Content-Type':'application/json',Authorization:'Bearer '+token}});const data=await r.json().catch(()=>({}));if(r.status===401){logout();throw Error('Ключ не прийнято');}if(!r.ok)throw Error(data.detail||'Помилка сервера');return data;}
async function load(){
  $('error').textContent='';
  try{
    const [data,overview]=await Promise.all([api('/admin/leads?limit=20&offset='+offset+($('filter').value?'&state='+$('filter').value:'')),api('/admin/overview')]);
    total=data.total;$('leads').replaceChildren();
    $('overview').textContent=`Каталог: ${overview.catalog.count} товарів, ${overview.catalog.fresh?'актуальний':'потребує оновлення'}. `+(overview.demo?'Демонстраційний режим: заявки локальні.':overview.telegram_configured?'Сповіщення Telegram налаштовано.':'Telegram не налаштовано — опрацьовуйте заявки в цьому кабінеті.');
    for(const lead of data.leads){
      const box=n('article');box.className='entry';const d=lead.details;
      box.append(n('h2',(d.name||'Клієнт')+' · '+states[lead.state]));const meta=n('p',new Date(lead.created*1000).toLocaleString('uk-UA')+' · '+lead.id+' · '+notices[lead.notify_state]);meta.className='meta';box.append(meta);
      const contact=n('p');const phone=n('a',d.phone);phone.href='tel:'+d.phone;contact.append(phone);box.append(contact);
      if(d.product)box.append(n('p','Товар: '+d.product.name+' · '+d.product.id));
      if(d.note)box.append(n('p','Питання: '+d.note));
      const details=n('details');details.append(n('summary','Контекст і згода'));details.append(n('pre',(d.context||[]).map(m=>(m.role==='user'?'Клієнт: ':'Консультант: ')+m.text).join('\n\n')+'\n\nЗгода: '+d.consent_version+' · '+new Date(d.consent_at*1000).toLocaleString('uk-UA')));box.append(details);
      const actions=n('div');actions.className='row';
      for(const [key,label] of Object.entries(states)){const b=n('button',label);b.disabled=lead.state===key;b.addEventListener('click',async()=>{try{await api('/admin/leads/'+lead.id,{method:'PATCH',body:JSON.stringify({state:key})});await load();}catch(e){$('error').textContent=e.message;}});actions.append(b);}
      box.append(actions);$('leads').append(box);
    }
    if(!data.leads.length)$('leads').append(n('p','Заявок за цим фільтром немає.'));
    $('page').textContent=total?`${offset+1}–${Math.min(offset+20,total)} із ${total}`:'0 заявок';$('prev').disabled=offset===0;$('next').disabled=offset+20>=total;
    $('workspace').hidden=false;$('login').hidden=true;
  }catch(e){$('error').textContent=e.message;}
}
$('login').addEventListener('submit',e=>{e.preventDefault();token=$('token').value.trim();$('token').value='';offset=0;load();});
$('logout').addEventListener('click',logout);$('refresh').addEventListener('click',load);$('filter').addEventListener('change',()=>{offset=0;load();});
$('prev').addEventListener('click',()=>{offset=Math.max(0,offset-20);load();});$('next').addEventListener('click',()=>{offset+=20;load();});
$('catalog-refresh').addEventListener('click',async()=>{try{await api('/admin/refresh-catalog',{method:'POST'});await load();}catch(e){$('error').textContent=e.message;}});
document.addEventListener('pointerdown',idle);document.addEventListener('keydown',idle);
})();
