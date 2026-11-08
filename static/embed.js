/* ABV AI Chat floating launcher for Horoshop. Copy this file's URL as a script src on the website. */
(() => {
  if(document.getElementById('abv-ai-embed-root'))return;
  const src=document.currentScript?.src;
  if(!src)return;
  const base=new URL(src).origin;
  const root=document.createElement('div');root.id='abv-ai-embed-root';
  const button=document.createElement('button');button.type='button';button.textContent='💬';
  button.setAttribute('aria-label','Відкрити онлайн-консультанта ABV');
  button.style.cssText='position:fixed;right:20px;bottom:24px;width:60px;height:60px;border:0;border-radius:50%;background:#0284c7;color:#fff;font-size:26px;cursor:pointer;box-shadow:0 6px 20px #0284c766;z-index:2147483000';
  const panel=document.createElement('div');
  panel.style.cssText='display:none;position:fixed;right:20px;bottom:94px;width:min(400px,calc(100vw - 24px));height:min(580px,calc(100dvh - 120px));z-index:2147483000;background:#fff;border-radius:16px;box-shadow:0 12px 35px #0003;overflow:hidden;border:1px solid #e2e8f0';
  const iframe=document.createElement('iframe');iframe.title='Онлайн-консультант ABV Himiya';iframe.loading='lazy';
  iframe.src=base+'/widget?embedded=1';iframe.style.cssText='width:100%;height:100%;border:0';
  panel.appendChild(iframe);
  button.addEventListener('click',()=>{const open=panel.style.display==='block';panel.style.display=open?'none':'block';button.textContent=open?'💬':'✕';button.setAttribute('aria-label',open?'Відкрити чат':'Закрити чат');});
  root.append(panel,button);document.body.appendChild(root);
})();
