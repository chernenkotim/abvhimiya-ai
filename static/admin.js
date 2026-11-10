'use strict';

(() => {
    // Елементи DOM
    const el = {
        leadsTab: document.getElementById('btnTabLeads'),
        dialogsTab: document.getElementById('btnTabDialogs'),
        leadsControls: document.getElementById('leads-controls'),
        dialogsControls: document.getElementById('dialogs-controls'),
        leadsContainer: document.getElementById('leads'),
        dialogsContainer: document.getElementById('dialogs'),
        filter: document.getElementById('filter'),
        overview: document.getElementById('overview'),
        error: document.getElementById('error'),
        page: document.getElementById('page'),
        prevBtn: document.getElementById('prev'),
        nextBtn: document.getElementById('next'),
        leadsCount: document.getElementById('leads-count'),
        dialogsCount: document.getElementById('dialogs-count'),
        pageTitle: document.getElementById('page-title')
    };

    // Стан додатку
    const state = {
        currentTab: 'leads', // 'leads' або 'dialogs'
        offset: 0,
        limit: 20,
        total: 0,
        refreshTimer: null
    };

    const LEAD_STATES = { new: 'Нова', in_progress: 'У роботі', done: 'Опрацьована' };
    const NOTICES = { pending: 'Очікує відправки', sending: 'Надсилається', sent: 'Надіслано в Telegram', needs_review: 'Помилка Telegram' };

    // Утиліти
    const escapeHtml = (str) => {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    };

    const showError = (msg) => {
        el.error.textContent = msg;
        setTimeout(() => el.error.textContent = '', 5000);
    };

    // Базовий API клієнт (підтримує Basic Auth через same-origin)
    async function api(path, options = {}) {
        options.credentials = 'same-origin';
        if (options.method && options.method !== 'GET') {
            options.headers = { ...options.headers, 'Content-Type': 'application/json' };
        }
        
        try {
            const res = await fetch(path, options);
            if (res.status === 401) {
                // Тригерить системне вікно Basic Auth
                window.location.reload(); 
                return;
            }
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                throw new Error(data.detail || `Помилка сервера: ${res.status}`);
            }
            return await res.json();
        } catch (err) {
            showError(err.message);
            throw err;
        }
    }

    // 1. Оновлення загальної статистики
    async function loadOverview() {
        try {
            const data = await api('/admin/overview');
            el.overview.innerHTML = `
                Каталог: <b>${data.catalog.count}</b> товарів<br>
                Telegram: ${data.telegram_configured ? '🟢 Налаштовано' : '🔴 Вимкнено'}
            `;
        } catch (e) {
            console.error("Overview error", e);
        }
    }

    // 2. Завантаження Заявок
    async function loadLeads(silent = false) {
        if (!silent) el.leadsContainer.innerHTML = '<div style="text-align:center; padding:20px; color:#64748b;">Завантаження...</div>';
        
        const filterVal = el.filter.value;
        const query = `?limit=${state.limit}&offset=${state.offset}${filterVal ? '&state=' + filterVal : ''}`;
        
        try {
            const data = await api('/admin/leads' + query);
            state.total = data.total;
            el.leadsCount.textContent = data.total;
            updatePagination();

            if (!data.leads || data.leads.length === 0) {
                el.leadsContainer.innerHTML = '<div class="card" style="text-align:center; color:#64748b;">Заявок немає.</div>';
                return;
            }

            el.leadsContainer.innerHTML = data.leads.map(l => {
                const p = l.details || {};
                const date = new Date(l.created * 1000).toLocaleString('uk-UA');
                const badgeClass = l.state; 

                const actionsHtml = Object.entries(LEAD_STATES).map(([key, label]) => {
                    if (l.state === key) return '';
                    const btnClass = key === 'done' ? 'btn-primary' : '';
                    return `<button class="${btnClass}" onclick="window.changeLeadState('${l.id}', '${key}')">${label}</button>`;
                }).join(' ');

                return `
                    <div class="card">
                        <div class="card-header">
                            <div>
                                <div style="font-size:16px; font-weight:600; margin-bottom:4px;">${escapeHtml(p.name || 'Клієнт без імені')}</div>
                                <div style="font-size:18px; color:var(--primary); font-weight:700;">
                                    <a href="tel:${escapeHtml(p.phone)}" style="text-decoration:none;">${escapeHtml(p.phone)}</a>
                                </div>
                            </div>
                            <div style="text-align:right;">
                                <span class="badge ${badgeClass}">${LEAD_STATES[l.state] || l.state}</span>
                                <div style="font-size:12px; color:var(--text-muted); margin-top:4px;">${date}</div>
                                <div style="font-size:11px; color:var(--text-muted); margin-top:2px;" title="Статус Telegram">${NOTICES[l.notify_state]}</div>
                            </div>
                        </div>
                        ${p.product ? `<div style="padding:8px; background:var(--bg); border-radius:6px; margin:8px 0;">🛒 Цікавить: <b>${escapeHtml(p.product.name)}</b> <span style="font-size:12px; color:var(--text-muted);">(Арт: ${escapeHtml(p.product.vendor_code || p.product.id)})</span></div>` : ''}
                        ${p.note ? `<div style="margin-top:8px;">📝 Коментар: <i>${escapeHtml(p.note)}</i></div>` : ''}
                        
                        <details style="margin-top: 12px; font-size:12px; color:var(--text-muted); cursor:pointer;">
                            <summary>Контекст діалогу</summary>
                            <pre style="background:var(--bg); padding:10px; border-radius:6px; white-space:pre-wrap; margin-top:8px;">${
                                (p.context || []).map(m => (m.role === 'user' ? '👤 Клієнт: ' : '🤖 Бот: ') + m.text).join('\n\n')
                            }</pre>
                        </details>

                        <div class="status-actions">
                            ${actionsHtml}
                        </div>
                    </div>
                `;
            }).join('');
        } catch (e) {
            el.leadsContainer.innerHTML = '<div class="card" style="color:#ef4444;">Помилка завантаження заявок.</div>';
        }
    }

    // 3. Завантаження Діалогів
    async function loadDialogs(silent = false) {
        if (!silent) el.dialogsContainer.innerHTML = '<div style="text-align:center; padding:20px; color:#64748b;">Завантаження...</div>';
        
        try {
            const data = await api(`/admin/dialogs-data?limit=${state.limit}&offset=${state.offset}`);
            state.total = data.total || 0;
            el.dialogsCount.textContent = state.total;
            updatePagination();

            if (!data.dialogs || data.dialogs.length === 0) {
                el.dialogsContainer.innerHTML = '<div class="card" style="text-align:center; color:#64748b;">Діалогів немає.</div>';
                return;
            }

            el.dialogsContainer.innerHTML = data.dialogs.map(d => {
                const dateStr = new Date(d.updated * 1000).toLocaleString('uk-UA');
                const msgs = d.messages || [];
                const firstUserMsg = msgs.find(m => m.role === 'user')?.text || 'Порожній діалог';

                const chatHtml = msgs.map(m => {
                    const isUser = m.role === 'user';
                    return `
                        <div class="chat-msg ${isUser ? 'msg-client' : 'msg-bot'}">
                            <div class="msg-header">${isUser ? 'Клієнт' : 'Асистент ABV'}</div>
                            <div>${escapeHtml(m.text)}</div>
                        </div>
                    `;
                }).join('');

                return `
                    <details class="dialog-card">
                        <summary>
                            <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 70%;">💬 "${escapeHtml(firstUserMsg)}"</span>
                            <span class="meta-tag">${dateStr} • ${msgs.length} повід.</span>
                        </summary>
                        <div class="chat-body">
                            ${chatHtml}
                        </div>
                    </details>
                `;
            }).join('');
        } catch (e) {
            el.dialogsContainer.innerHTML = '<div class="card" style="color:#ef4444;">Помилка завантаження діалогів.</div>';
        }
    }

    // Зміна стану заявки (Глобально, щоб викликати з inline HTML)
    window.changeLeadState = async function(id, newState) {
        try {
            await api(`/admin/leads/${id}`, { method: 'PATCH', body: JSON.stringify({ state: newState }) });
            loadLeads(true);
        } catch (e) {
            console.error(e);
        }
    };

    // Навігація по вкладках
    function switchTab(tabName) {
        state.currentTab = tabName;
        state.offset = 0; // Скидаємо сторінку при перемиканні
        
        const isLeads = tabName === 'leads';
        el.leadsTab.classList.toggle('active', isLeads);
        el.dialogsTab.classList.toggle('active', !isLeads);
        
        el.leadsControls.style.display = isLeads ? 'flex' : 'none';
        el.dialogsControls.style.display = isLeads ? 'none' : 'flex';
        
        el.leadsContainer.style.display = isLeads ? 'block' : 'none';
        el.dialogsContainer.style.display = isLeads ? 'none' : 'block';
        
        el.pageTitle.innerText = isLeads ? 'Заявки з чату' : 'Живі діалоги з покупцями';

        refreshData();
    }
    window.switchTab = switchTab;

    // Пагінація
    function updatePagination() {
        const pageNum = Math.floor(state.offset / state.limit) + 1;
        const totalPages = Math.ceil(state.total / state.limit) || 1;
        
        el.page.textContent = `Сторінка ${pageNum} з ${totalPages}`;
        el.prevBtn.disabled = state.offset === 0;
        el.nextBtn.disabled = state.offset + state.limit >= state.total;
    }

    window.changePage = function(delta) {
        const newOffset = state.offset + (delta * state.limit);
        if (newOffset < 0 || newOffset >= state.total) return;
        
        state.offset = newOffset;
        refreshData();
        window.scrollTo({ top: 0, behavior: 'smooth' });
    };

    // Загальне оновлення
    function refreshData(silent = false) {
        loadOverview();
        if (state.currentTab === 'leads') loadLeads(silent);
        else loadDialogs(silent);
    }
    window.refreshData = refreshData;

    // Оновлення каталогу (захист від подвійного кліку)
    window.refreshCatalog = async function(btnEvent) {
        const btn = btnEvent ? btnEvent.target : document.querySelector('button[onclick="refreshCatalog(event)"]');
        if(btn) { btn.disabled = true; btn.textContent = 'Оновлюю...'; }
        
        try {
            await api('/admin/refresh-catalog', { method: 'POST' });
            loadOverview();
            alert('✅ Каталог успішно оновлено!');
        } catch (e) {
            console.error(e);
        } finally {
            if(btn) { btn.disabled = false; btn.textContent = 'Оновити каталог'; }
        }
    };

    // Прив'язка подій
    el.filter.addEventListener('change', () => { state.offset = 0; loadLeads(); });

    // Автоматичне фонове оновлення кожні 15 секунд (тільки для 1-ї сторінки)
    state.refreshTimer = setInterval(() => {
        if (state.offset === 0) refreshData(true);
    }, 15000);

    // Ініціалізація
    refreshData();
})();
