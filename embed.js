/* Embed once on the shop: <script defer src="https://CHAT-DOMAIN/static/embed.js"></script> */
(() => {
  if(document.getElementById('abv-ai-embed-root'))return;
  const src=document.currentScript?.src;if(!src)return;
  const base=new URL(src).origin;
  const root=document.createElement('div');root.id='abv-ai-embed-root';
  const button=document.createElement('button');button.type='button';button.textContent='💬';button.setAttribute('aria-label','Відкрити чат ABV Хімія');button.setAttribute('aria-expanded','false');
  button.style.cssText='position:fixed;right:18px;bottom:22px;width:58px;height:58px;border:0;border-radius:50%;background:#0868a6;color:#fff;font-size:25px;cursor:pointer;box-shadow:0 5px 24px #12436533;z-index:2147483000';
  const panel=document.createElement('div');panel.id='abv-ai-panel';button.setAttribute('aria-controls',panel.id);
  panel.style.cssText='display:none;position:fixed;right:12px;bottom:90px;width:min(430px,calc(100vw - 24px));height:min(700px,calc(100dvh - 110px));z-index:2147483000;border-radius:18px;overflow:hidden;box-shadow:0 10px 60px #102a4355;background:white';
  let frame;
  function toggle(open){
    if(open&&!frame){frame=document.createElement('iframe');frame.title='Чат-консультант ABV Хімія';frame.src=base+'/widget?embedded=1';frame.referrerPolicy='strict-origin-when-cross-origin';frame.style.cssText='width:100%;height:100%;border:0';panel.append(frame);}
    panel.style.display=open?'block':'none';button.textContent=open?'×':'💬';button.setAttribute('aria-expanded',String(open));button.setAttribute('aria-label',open?'Закрити чат':'Відкрити чат ABV Хімія');if(!open)button.focus();
  }
  button.addEventListener('click',()=>toggle(panel.style.display!=='block'));
  window.addEventListener('message',e=>{if(frame&&e.origin===base&&e.source===frame.contentWindow&&e.data?.type==='abv:close')toggle(false);});
  window.addEventListener('keydown',e=>{if(e.key==='Escape'&&panel.style.display==='block')toggle(false);});
  root.append(panel,button);document.body.append(root);
})();
