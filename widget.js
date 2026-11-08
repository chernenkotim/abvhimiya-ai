'use strict';
// DOM rendering only. Never interpolate model-generated text into innerHTML.
(() => {
const $ = id => document.getElementById(id);
const box = $('ai-chat-messages');
const inp = $('ai-chat-input');
let sessionId = null;
let orderItem = null;
let busy = false;
let submitting = false;
let requestId = null;
const makeId = () => (globalThis.crypto?.randomUUID?.() || `req_${Date.now()}_${Math.random().toString(36).slice(2)}`);
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (typeof text === 'string') node.textContent = text;
  if (className) node.className = className;
  return node;
}
function appendMessage(message, who='bot') {
  const msg=el('div',message,`msg ${who}`);
  box.append(msg); box.scrollTop=box.scrollHeight;
  return msg;
}
async function ensureSession() {
  if (sessionId) return sessionId;
  let saved;
  try { saved=sessionStorage.getItem('abv_chat_session_v2'); } catch (_) {}
  if (saved) { sessionId=saved; return saved; }
  const r=await fetch('/api/session',{method:'POST'});
  if (!r.ok) throw Error('Не вдалося відкрити сесію');
  const data=await r.json();
  sessionId=data.session_id;
  try { sessionStorage.setItem('abv_chat_session_v2',sessionId); } catch (_) {}
  return sessionId;
}
async function post(url, body) {
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),25000);
  try {
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:controller.signal});
    const data=await r.json().catch(()=>({}));
    if (!r.ok) throw Error(typeof data.detail === 'string'? data.detail : `Сервер повернув код ${r.status}`);
    return data;
  } finally {clearTimeout(timer);}
}
function renderProduct(p) {
  const card=el('div',null,'prod-card');
  card.append(el('div',p.name,'prod-title'));
  const meta=el('div',null,'prod-meta');
  meta.append(el('span',`${Number(p.price).toLocaleString('uk-UA')} грн`,'prod-price'));
  meta.append(el('span',p.available?'🟢 В наявності':'🟡 Немає в наявності',p.available?'badge-instock':'badge-outstock'));
  card.append(meta);
  const actions=el('div',null,'prod-actions');
  const link=el('a','Картка ↗','btn-view');
  try {
    const parsed=new URL(p.url);
    if(parsed.protocol==='https:' && ['abvhimiya.com.ua','www.abvhimiya.com.ua','sunlu.kyiv.ua','www.sunlu.kyiv.ua'].includes(parsed.hostname)) {
      link.href=parsed.href;link.target='_blank';link.rel='noopener noreferrer';
    } else { link.removeAttribute('href'); }
  } catch (_) {link.removeAttribute('href');}
  actions.append(link);
  const order=el('button','⚡ В 1 клік','btn-order-fast');
  order.type='button';order.disabled=!p.available;
  order.addEventListener('click',()=>openOrder(p));
  actions.append(order);card.append(actions);box.append(card);
  box.scrollTop=box.scrollHeight;
}
async function sendMsg() {
  if(busy)return;
  const text=inp.value.trim();if(!text)return;
  inp.value='';busy=true;$('ai-chat-send').disabled=true;
  appendMessage(text,'user');
  const placeholder=appendMessage('Секунду, уточнюю інформацію...');
  try {
    let data;
    try {data=await post('/api/chat',{message:text,session_id:await ensureSession()});}
    catch(e) {
      if(/Сесія недійсна/.test(e.message)){
        sessionId=null;try{sessionStorage.removeItem('abv_chat_session_v2');}catch(_){ }
        data=await post('/api/chat',{message:text,session_id:await ensureSession()});
      } else throw e;
    }
    placeholder.textContent=data.reply || 'Не вдалося отримати відповідь.';
    for(const p of data.products||[])renderProduct(p);
    if(data.handoff){
      const btn=el('button','📞 Залишити звернення менеджеру','quick-chip');
      btn.addEventListener('click',()=>openOrder(null));box.append(btn);
    }
  } catch(e){placeholder.textContent=e.message || 'Помилка зв’язку з сервером.';}
  finally{busy=false;$('ai-chat-send').disabled=false;box.scrollTop=box.scrollHeight;}
}
function openOrder(product) {
  orderItem=product;requestId=makeId();
  $('modal-title').textContent=product?'⚡ Швидке замовлення':'📞 Зв’язатися з менеджером';
  $('modal-product-name').textContent=product?`${product.name} — ${Number(product.price).toLocaleString('uk-UA')} грн`:'Залиште телефон, і менеджер зв’яжеться з вами.';
  $('modal-submit').textContent=product?'Замовити':'Надіслати';
  $('modal-error').textContent='';$('fast-consent').checked=false;
  $('fast-order-modal').style.display='flex';$('fast-name').focus();
}
function closeOrder(){$('fast-order-modal').style.display='none';}
async function submitOrder() {
  if(submitting)return;
  const name=$('fast-name').value.trim();const phone=$('fast-phone').value.trim();
  if(!phone){$('modal-error').textContent='Вкажіть номер телефону';return;}
  if(!$('fast-consent').checked){$('modal-error').textContent='Підтвердьте згоду на обробку даних';return;}
  submitting=true;$('modal-submit').disabled=true;$('modal-error').textContent='';
  try {
    const sid=await ensureSession();
    const body={name,phone,session_id:sid,request_id:requestId,consent:true};
    const data=orderItem ? await post('/api/quick-order',{...body,product_id:orderItem.id,quantity:1})
                         : await post('/api/manager-request',{...body,note:'Звернення через AI-чат'});
    closeOrder();appendMessage(data.message || 'Звернення збережено.');
    $('fast-phone').value='';$('fast-name').value='';
  } catch(e){$('modal-error').textContent=e.message || 'Помилка надсилання. Спробуйте ще раз.';}
  finally{submitting=false;$('modal-submit').disabled=false;}
}
function toggle(){
  const win=$('ai-chat-window');$('ai-proactive-bubble').style.display='none';
  win.style.display=win.style.display==='flex'?'none':'flex';
  if(win.style.display==='flex')inp.focus();
}
if(new URLSearchParams(location.search).has('embedded')) {
  document.body.classList.add('abv-embedded');
  $('ai-chat-window').style.display='flex';
}
$('ai-chat-launcher').addEventListener('click',toggle);
$('ai-chat-launcher').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle();}});
$('ai-chat-close').addEventListener('click',toggle);
$('ai-proactive-bubble').addEventListener('click',()=>{if($('ai-chat-window').style.display!=='flex')toggle();});
$('ai-chat-send').addEventListener('click',sendMsg);
inp.addEventListener('keydown',e=>{if(e.key==='Enter')sendMsg();});
for(const chip of document.querySelectorAll('[data-question]')) chip.addEventListener('click',()=>{inp.value=chip.dataset.question;sendMsg();});
for(const chip of document.querySelectorAll('[data-manager]')) chip.addEventListener('click',()=>openOrder(null));
$('modal-cancel').addEventListener('click',closeOrder);
$('modal-submit').addEventListener('click',submitOrder);
setTimeout(()=>{if(!$('ai-chat-window').classList.contains('hidden') && $('ai-chat-window').style.display!=='flex' && !document.body.classList.contains('abv-embedded'))$('ai-proactive-bubble').style.display='block';},12000);
})();
