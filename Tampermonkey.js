// ==UserScript==
// @name         DeepSeek Agent (基于 /file 接口)
// @namespace    http://tampermonkey.net/
// @version      9.1
// @description  利用虚拟列表奇偶定位AI回复，调用 /file 接口执行文件操作，悬浮按钮可拖动，含重置输入框
// @author       小马
// @match        https://chat.deepseek.com/*
// @grant        GM_xmlhttpRequest
// ==/UserScript==

(function () {
    'use strict';

    // ============ 配置 ============
    const BASE_URL = 'http://localhost:8888';
    const FILE_API = BASE_URL + '/file';
    const MAX_RETRY = 3;
    const MAX_ITERATIONS = 200;
    const SILENT_WAIT = 2800;
    const DEFAULT_PROMPT = `你好`;

    // ============ 日志工具 ============
    function logInfo(...args) { console.log('[Agent]', ...args); }
    function logWarn(...args) { console.warn('[Agent]', ...args); }
    function logError(...args) { console.error('[Agent]', ...args); }

    // ============ 状态 ============
    let isRunning = false;
    let iteration = 0;
    let processing = false;
    let lastMsg = '';
    let retryCount = 0;
    let currentPlan = null;
    let observer = null;
    let messageTimers = new Map();
    let lastProcessedText = '';
    let lastFoundText = '';
    let lastAIMessageKey = -1;

    // ============ 核心：通过虚拟列表 key 获取最新 AI 消息 ============
    function getLatestAIMessage() {
        const items = document.querySelectorAll('[data-virtual-list-item-key]');
        if (!items.length) return null;

        let aiItems = [];
        for (const el of items) {
            const key = parseInt(el.getAttribute('data-virtual-list-item-key'), 10);
            if (!isNaN(key) && key % 2 === 0) {
                aiItems.push({ key, element: el });
            }
        }
        if (aiItems.length === 0) return null;

        aiItems.sort((a, b) => a.key - b.key);
        const latest = aiItems[aiItems.length - 1];
        if (latest.key === lastAIMessageKey) {
            return null;
        }

        let contentElement = latest.element.querySelector('.ds-markdown.ds-assistant-message-main-content');
        let text = contentElement ? contentElement.textContent : '';
        text = text.trim();
        return { text, element: latest.element, key: latest.key };
    }

    top.window.debugAgent = getLatestAIMessage;

    function parsePlan(text) {
        let jsonStr = text.trim();
        if (!jsonStr) return null;
        if (jsonStr.startsWith("```json") && jsonStr.endsWith("```")) {
            jsonStr = jsonStr.slice(7, -3);
        }
        if (jsonStr.startsWith("{") && jsonStr.endsWith("}")) {
            try {
                const plan = JSON.parse(jsonStr);
                if (plan.type === 'file') {
                    const allowedActions = ['create', 'read', 'delete', 'list', 'overwrite', 'append', 'replace'];
                    if (!plan.action || !allowedActions.includes(plan.action)) {
                        logWarn('计划缺少 action 或 action 无效');
                        return null;
                    }
                    if (plan.action !== 'list' && !plan.path) {
                        logWarn('非 list 操作缺少 path 字段');
                        return null;
                    }
                }
                return plan;
            } catch (e) {
                logError('JSON解析失败:', e);
                return 'JSON_ERROR ' + e.message;
            }
        }
        return null;
    }

    function isComplete(text) {
        return text.includes('===TASK_COMPLETE===') || iteration >= MAX_ITERATIONS;
    }

    function sendMessage(text) {
        logInfo('📤 发送消息:', text.slice(0, 200) + (text.length > 200 ? '...' : ''));
        const ta = document.querySelector('textarea[placeholder*="发送消息"]');
        if (!ta) { logError('未找到输入框'); return; }
        ta.focus();

        let success = false;
        if (document.execCommand) {
            try { success = document.execCommand('insertText', false, text); } catch (e) {}
        }
        if (!success) {
            ta.value = text;
            ta.dispatchEvent(new Event('input', { bubbles: true }));
            ta.dispatchEvent(new Event('change', { bubbles: true }));
            ta.dispatchEvent(new Event('compositionend', { bubbles: true }));
        }

        setTimeout(() => {
            let sendBtn = document.querySelector('div[role="button"].ds-button--primary.ds-button--circle:not(.ds-button--disabled)');
            if (!sendBtn) {
                const svg = document.querySelector('svg path[d*="M8.3125 0.981587"]');
                if (svg) {
                    const parent = svg.closest('div[role="button"]');
                    if (parent && !parent.classList.contains('ds-button--disabled')) sendBtn = parent;
                }
            }
            if (sendBtn) {
                sendBtn.click();
                logInfo('✅ 点击发送按钮');
                setTimeout(() => {
                    if (ta.value !== '') {
                        ta.value = '';
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }, 100);
            } else {
                ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
                ta.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
                logInfo('⚠️ 未找到发送按钮，模拟 Enter 键');
            }
        }, 400);
    }

    function callFileAPI(plan, callback) {
        let payload = { action: plan.action };
        if (plan.action !== 'list') payload.path = plan.path || '';

        if (plan.action === 'create' || plan.action === 'overwrite' || plan.action === 'append') {
            payload.content = plan.content || '';
        } else if (plan.action === 'replace') {
            if (!plan.search || plan.replace === undefined) {
                callback(new Error('replace 操作缺少 search 或 replace 字段'), null);
                return;
            }
            payload.search = plan.search;
            payload.replace = plan.replace;
            if (plan.count !== undefined) payload.count = plan.count;
        }

        logInfo(`🚀 发送文件操作: ${payload.action} ${payload.path || ''}`);
        GM_xmlhttpRequest({
            method: 'POST',
            url: FILE_API,
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify(payload),
            onload: function (res) {
                try {
                    const result = JSON.parse(res.responseText);
                    logInfo('✅ 操作结果:', result);
                    callback(null, result);
                } catch (e) {
                    logError('解析响应失败:', e);
                    callback(e, null);
                }
            },
            onerror: function (err) { callback(err, null); },
            ontimeout: function () { callback(new Error('请求超时'), null); },
            timeout: 30000
        });
    }

    function executePlan(plan) {
        logInfo(`执行计划 (重试 ${retryCount}/${MAX_RETRY})`, plan);
        callFileAPI(plan, function (err, result) {
            if (err) {
                logError('执行失败:', err);
                retryCount++;
                if (retryCount < MAX_RETRY) {
                    const delay = retryCount * 2000;
                    logInfo(`${delay}ms 后重试...`);
                    setTimeout(() => executePlan(plan), delay);
                } else {
                    sendMessage(`[Agent] ❌ 操作失败，已重试 ${MAX_RETRY} 次。错误: ${err.message || '未知错误'}`);
                    processing = false;
                }
                return;
            }

            let summary = '';
            if (result.status === 'success') {
                if (plan.action === 'list') {
                    const listData = result.data || { files: [] };
                    const files = Array.isArray(listData.files) ? listData.files : [];
                    const total = listData.total !== undefined ? listData.total : files.length;
                    summary = `📂 当前文件列表 (共 ${total} 个):\n` +
                        files.map(f => `  - ${f.path} (${f.size} bytes)`).join('\n');
                } else {
                    const data = result.data || {};
                    summary = `✅ 操作成功: ${plan.action} ${plan.path}\n` +
                        `  路径: ${data.path || plan.path}\n` +
                        (data.size ? `  大小: ${data.size} bytes` : '') +
                        (data.content ? `\n内容预览: ${data.content}` : '');
                }
            } else {
                summary = `❌ 操作失败: ${result.message || '未知错误'}`;
            }

            sendMessage(`执行结果：\n${summary}`);
            processing = false;
        });
    }

    function processAIResponse(text) {
        if (!isRunning || processing) return;
        if (!text || text === lastMsg) return;

        lastMsg = text;
        processing = true;
        iteration++;

        logInfo(`📥 收到AI回复 (长度: ${text.length})`);
        logInfo('内容预览:', text.slice(0, 100) + (text.length > 100 ? '...' : ''));

        if (isComplete(text)) {
            const msg = iteration >= MAX_ITERATIONS ? `⚠️ 达到最大迭代次数(${MAX_ITERATIONS})，自动停止` : '✅ 任务已完成！';
            logInfo(`[Agent] ${msg}`);
            processing = false;
            return;
        }

        const plan = parsePlan(text);
        if (plan === null) {
            logInfo('未检测到有效计划，等待下一个响应');
            processing = false;
            return;
        }
        if (typeof plan === 'string' && plan.startsWith('JSON_ERROR')) {
            sendMessage(plan);
            processing = false;
            return;
        }

        currentPlan = plan;
        retryCount = 0;
        logInfo('解析到计划，开始执行', plan);
        executePlan(plan);
    }

    function setupDOMObserver() {
        if (observer) {
            observer.disconnect();
            observer = null;
        }
        for (const timer of messageTimers.values()) clearTimeout(timer);
        messageTimers.clear();

        observer = new MutationObserver(() => {
            if (!isRunning || processing) return;
            const msg = getLatestAIMessage();
            if (!msg) return;
            const currentText = msg.text;
            if (msg.key === lastAIMessageKey && currentText === lastProcessedText) return;

            const msgId = 'ai_' + msg.key;
            if (messageTimers.has(msgId)) clearTimeout(messageTimers.get(msgId));

            const timer = setTimeout(() => {
                messageTimers.delete(msgId);
                const latest = getLatestAIMessage();
                if (!latest) return;
                const finalText = latest.text;
                if (finalText === lastProcessedText) return;
                if (finalText.length < 10) return;

                lastProcessedText = finalText;
                lastAIMessageKey = latest.key;
                logInfo('静默期结束，处理AI回复', finalText.slice(0, 100));
                processAIResponse(finalText);
            }, SILENT_WAIT + Math.random() * 10);

            messageTimers.set(msgId, timer);
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true,
            characterData: true,
            characterDataOldValue: false,
            attributes: true,
            attributeFilter: ['data-virtual-list-item-key']
        });

        logInfo(`DOM 监听已启动（静默期 ${SILENT_WAIT}ms）`);
        setTimeout(getLatestAIMessage, 2000);
    }

    function sendPromptOnce(prompt) {
        let pathname = location.pathname.length > 10 ? location.pathname : Math.random();
        if (!sessionStorage.getItem('agent_prompt_sent' + pathname)) {
            sessionStorage.setItem('agent_prompt_sent' + pathname, 'true');
            setTimeout(() => {
                sendMessage(prompt);
                logInfo('系统提示词已发送');
            }, 1500);
        }
    }

    function startAgent(skipPrompt) {
        logInfo('启动 Agent');
        iteration = 0;
        lastMsg = '';
        retryCount = 0;
        currentPlan = null;
        processing = false;
        lastProcessedText = '';
        lastFoundText = '';
        lastAIMessageKey = -1;

        if (!(location.pathname || '').length < 5 && !skipPrompt) {
            GM_xmlhttpRequest({
                method: 'GET',
                url: BASE_URL + '/prompt',
                onload: function (res) {
                    try {
                        const data = JSON.parse(res.responseText);
                        let prompt = data?.data?.prompt;
                        if (!prompt) {
                            prompt = DEFAULT_PROMPT;
                            logInfo('使用内置的新格式提示词');
                        } else {
                            logInfo('获取到服务端提示词');
                        }
                        sendPromptOnce(prompt);
                    } catch (e) {
                        logWarn('解析提示词失败，使用默认新格式');
                        sendPromptOnce(DEFAULT_PROMPT);
                    }
                },
                onerror: function () {
                    logWarn('获取提示词失败，使用默认新格式');
                    sendPromptOnce(DEFAULT_PROMPT);
                }
            });
        }
        setupDOMObserver();
    }

    function stopAgent() {
        logInfo('停止 Agent');
        isRunning = false;
        processing = false;
        if (observer) observer.disconnect();
        const btn = document.querySelector('.agent-toggle-btn');
        if (btn) {
            btn.querySelector('span').textContent = 'Agent';
            btn.style.backgroundColor = '';
        }
        logInfo('已停止');
    }

    // ============ 创建悬浮按钮容器（可拖动） ============
    function createFloatingButtons() {
        if (document.querySelector('.agent-float-container')) return;

        const container = document.createElement('div');
        container.className = 'agent-float-container';
        container.style.cssText = 'position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); z-index: 9999; display: flex; gap: 8px; cursor: move; user-select: none;';
        document.body.appendChild(container);

        // ---- Agent 开关按钮 ----
        const btn = document.createElement('div');
        btn.className = 'f79352dc ds-toggle-button ds-toggle-button--m agent-toggle-btn';
        btn.style.cssText = 'cursor: pointer; padding: 6px 10px; background: #f3f4f6; border-radius: 18px; display: flex; align-items: center; gap: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.2);';
        btn.setAttribute('role', 'button');
        btn.setAttribute('tabindex', '0');
        btn.innerHTML = `
            <div class="ds-toggle-button__icon">
                <div class="ds-icon" style="font-size: inherit;">
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                        <path d="M8 2C4.686 2 2 4.686 2 8s2.686 6 6 6 6-2.686 6-6-2.686-6-6-6zm0 10.5c-2.485 0-4.5-2.015-4.5-4.5S5.515 3.5 8 3.5s4.5 2.015 4.5 4.5-2.015 4.5-4.5 4.5z"/>
                        <circle cx="8" cy="8" r="2" fill="currentColor"/>
                    </svg>
                </div>
            </div>
            <span class="_6dbc175" style="font-size:13px;">Agent</span>
        `;

        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (isRunning) {
                stopAgent();
            } else {
                const span = this.querySelector('span._6dbc175');
                if (span) span.textContent = '停止';
                this.style.backgroundColor = '#3964fe';
                isRunning = true;
                startAgent();
            }
        });

        // ---- 重置/输入按钮 ----
        const resetBtn = document.createElement('div');
        resetBtn.className = 'agent-reset-btn';
        resetBtn.style.cssText = 'cursor: pointer; padding: 6px 10px; background: #ffffff; border-radius: 18px; display: flex; align-items: center; gap: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.2);';
        resetBtn.setAttribute('role', 'button');
        resetBtn.innerHTML = `
            <span style="font-size:13px;">📝 重置</span>
        `;

        resetBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            const modal = document.createElement('div');
            modal.style.cssText = 'position: fixed; bottom: 20%; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.6); padding: 20px; border-radius: 12px; z-index: 10001; min-width: 400px; display: flex; flex-direction: column; gap: 12px;';
            const input = document.createElement('textarea');
            input.placeholder = '输入要发送的内容... (Ctrl+Enter发送)';
            input.style.cssText = 'background: rgba(255,255,255,0.9); width: 100%; height: 120px; padding: 10px; font-size: 14px; border-radius: 6px; border: none; resize: vertical; color: #333;';
            const btnContainer = document.createElement('div');
            btnContainer.style.cssText = 'display: flex; gap: 10px; justify-content: center;';
            const sendBtn = document.createElement('button');
            sendBtn.textContent = '发送 (Ctrl+Enter)';
            sendBtn.style.cssText = 'padding: 8px 20px; cursor: pointer; border: none; border-radius: 6px; background: #3964fe; color: white; font-size: 14px;';
            const closeBtn = document.createElement('button');
            closeBtn.textContent = '取消';
            closeBtn.style.cssText = 'padding: 8px 20px; cursor: pointer; border: none; border-radius: 6px; background: #ccc; color: #333; font-size: 14px;';
            const overlay = document.createElement('div');
            overlay.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.2); z-index: 9999;';
            function sendText() {
                const text = input.value.trim();
                if (text) {
                    iteration = 0;
                    lastAIMessageKey = -1;
                    lastProcessedText = '';
                    lastMsg = '';
                    processing = false;
                    sendMessage(text);
                    logInfo('已发送输入内容并重置步骤');
                    input.value = '';
                }
            }
            function closeModal() {
                overlay.remove();
                modal.remove();
            }
            input.addEventListener('keydown', function(e) {
                if (e.ctrlKey && e.key === 'Enter') {
                    e.preventDefault();
                    sendText();
                }
            });
            sendBtn.addEventListener('click', sendText);
            closeBtn.addEventListener('click', closeModal);
            overlay.addEventListener('click', closeModal);
            btnContainer.appendChild(sendBtn);
            btnContainer.appendChild(closeBtn);
            modal.appendChild(input);
            modal.appendChild(btnContainer);
            document.body.appendChild(overlay);
            document.body.appendChild(modal);
            input.focus();
        });

        container.appendChild(btn);
        container.appendChild(resetBtn);

        // ---- 拖动功能 ----
        let isDragging = false;
        let startX, startY, origX, origY;

        container.addEventListener('mousedown', function (e) {
            // 只有点击容器本身或空白区域才触发拖动，避免影响按钮点击
            if (e.target === container) {
                isDragging = true;
                startX = e.clientX;
                startY = e.clientY;
                const rect = container.getBoundingClientRect();
                origX = rect.left;
                origY = rect.top;
                e.preventDefault();
            }
        });

        document.addEventListener('mousemove', function (e) {
            if (!isDragging) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            container.style.left = (origX + dx) + 'px';
            container.style.top = (origY + dy) + 'px';
            container.style.right = 'auto';
            container.style.bottom = 'auto';
        });

        document.addEventListener('mouseup', function () {
            isDragging = false;
        });
    }

    // ============ 初始化 ============
    function init() {
        if (document.querySelector('textarea[placeholder*="发送消息"]')) {
            createFloatingButtons();
        } else {
            setTimeout(init, 2000);
        }
    }
    init();
})();