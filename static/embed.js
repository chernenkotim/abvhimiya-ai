/* Embed once on the shop: <script defer src="https://CHAT-DOMAIN/static/embed.js" data-color="#0868a6"></script> */
(() => {
    if (document.getElementById('abv-ai-embed-root')) return;
    
    const scriptTag = document.currentScript;
    const src = scriptTag?.src;
    if (!src) return;
    
    const base = new URL(src).origin;
    // Читаємо колір з data-атрибута скрипта, або беремо фірмовий за замовчуванням
    const brandColor = scriptTag.dataset.color || '#0868a6';
    
    // Іконки (SVG)
    const iconChat = `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>`;
    const iconClose = `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>`;

    // Інжект стилів для анімацій та мобільної адаптації
    const style = document.createElement('style');
    style.textContent = `
        #abv-ai-embed-root {
            --abv-color: ${brandColor};
            --abv-z: 2147483000;
        }
        #abv-ai-button {
            position: fixed; right: 20px; bottom: 20px; width: 60px; height: 60px;
            border: none; border-radius: 50%; background: var(--abv-color); color: #fff;
            cursor: pointer; box-shadow: 0 4px 20px rgba(0,0,0,0.15); z-index: var(--abv-z);
            display: flex; align-items: center; justify-content: center;
            transition: transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275), box-shadow 0.2s;
        }
        #abv-ai-button:hover {
            transform: scale(1.08); box-shadow: 0 6px 24px rgba(0,0,0,0.2);
        }
        #abv-ai-panel {
            position: fixed; right: 20px; bottom: 95px; width: 400px; height: min(700px, calc(100dvh - 120px));
            z-index: var(--abv-z); border-radius: 16px; overflow: hidden;
            box-shadow: 0 10px 40px rgba(0,0,0,0.15); background: white;
            /* Анімація появи */
            opacity: 0; transform: translateY(20px); pointer-events: none;
            transition: opacity 0.3s ease, transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }
        #abv-ai-panel.abv-open {
            opacity: 1; transform: translateY(0); pointer-events: auto;
        }
        /* На мобільних розгортаємо на весь екран, щоб клавіатура не ламала iframe */
        @media (max-width: 480px) {
            #abv-ai-panel {
                right: 0; bottom: 0; width: 100vw; height: 100dvh; border-radius: 0;
            }
            #abv-ai-button {
                right: 16px; bottom: 16px;
            }
        }
    `;
    document.head.append(style);

    const root = document.createElement('div');
    root.id = 'abv-ai-embed-root';
    
    const button = document.createElement('button');
    button.id = 'abv-ai-button';
    button.innerHTML = iconChat;
    button.setAttribute('aria-label', 'Відкрити чат ABV Хімія');
    button.setAttribute('aria-expanded', 'false');
    
    const panel = document.createElement('div');
    panel.id = 'abv-ai-panel';
    button.setAttribute('aria-controls', panel.id);
    
    let frame;
    
    function toggle(open) {
        if (open && !frame) {
            frame = document.createElement('iframe');
            frame.title = 'Чат-консультант ABV Хімія';
            // Передаємо URL сторінки, щоб бот міг консультувати по поточному товару
            const currentUrl = encodeURIComponent(window.location.href);
            frame.src = `${base}/widget?embedded=1&page=${currentUrl}`;
            frame.referrerPolicy = 'strict-origin-when-cross-origin';
            frame.style.cssText = 'width: 100%; height: 100%; border: 0; background: transparent;';
            panel.append(frame);
        }
        
        if (open) {
            panel.classList.add('abv-open');
            button.innerHTML = iconClose;
            button.setAttribute('aria-label', 'Закрити чат');
        } else {
            panel.classList.remove('abv-open');
            button.innerHTML = iconChat;
            button.setAttribute('aria-label', 'Відкрити чат ABV Хімія');
            button.focus();
        }
        button.setAttribute('aria-expanded', String(open));
    }
    
    button.addEventListener('click', () => toggle(!panel.classList.contains('abv-open')));
    
    // Закриття через повідомлення з iframe (наприклад, кнопкою хрестика всередині чату)
    window.addEventListener('message', e => {
        if (frame && e.origin === base && e.source === frame.contentWindow && e.data?.type === 'abv:close') {
            toggle(false);
        }
    });
    
    // Закриття клавішею Escape
    window.addEventListener('keydown', e => {
        if (e.key === 'Escape' && panel.classList.contains('abv-open')) {
            toggle(false);
        }
    });
    
    root.append(panel, button);
    document.body.append(root);
})();
