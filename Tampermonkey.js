// ==UserScript==
// @name         DeepSeek Agent (基于 WebSocket)
// @namespace    http://tampermonkey.net/
// @version      10.0
// @description  无遮罩、无标题、透明背景模态框，拖动整个空白区域，拖动提示前置；全部通信走 WebSocket，获取提示词失败弹框确认
// @author       小马
// @match        https://chat.deepseek.com/*
// @grant        none
// @require      https://cdn.jsdelivr.net/npm/json5@2/dist/index.min.js
// ==/UserScript==

(function () {
    'use strict';

    // ============ 配置 ============
    const WS_URL = 'ws://localhost:8765';
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

    // WebSocket 相关状态
    let ws = null;
    let wsConnected = false;
    let wsRequestId = 0;
    let pendingRequests = new Map();
    let wsReconnectTimer = null;
    let wsReconnectAttempts = 0;
    const WS_MAX_RECONNECT_ATTEMPTS = 10;
    const WS_RECONNECT_DELAY = 3000;

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
        let searchString = "json复制下载";
        if (jsonStr.startsWith(searchString)) {
            jsonStr = jsonStr.slice(searchString.length);
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

    // ============ WebSocket 客户端 ============
    function connectWebSocket() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
            logInfo('WebSocket 已连接或正在连接');
            return;
        }

        logInfo(`🔄 尝试连接 WebSocket: ${WS_URL}`);
        try {
            ws = new WebSocket(WS_URL);
        } catch (e) {
            logError('创建 WebSocket 失败:', e);
            scheduleReconnect();
            return;
        }

        ws.onopen = function () {
            wsConnected = true;
            wsReconnectAttempts = 0;
            logInfo('✅ WebSocket 已连接');
            for (const [id, pending] of pendingRequests) {
                clearTimeout(pending.timer);
                pending.reject(new Error('WebSocket 重连，请求已取消'));
                pendingRequests.delete(id);
            }
        };

        ws.onmessage = function (event) {
            let data;
            try {
                data = JSON.parse(event.data);
            } catch (e) {
                logError('WebSocket 收到非 JSON 消息:', event.data);
                return;
            }

            const requestId = data.id;
            if (requestId && pendingRequests.has(requestId)) {
                const pending = pendingRequests.get(requestId);
                clearTimeout(pending.timer);
                pendingRequests.delete(requestId);
                pending.resolve(data);
            } else {
                logInfo('收到未关联请求的响应:', data);
            }
        };

        ws.onerror = function (err) {
            logError('WebSocket 错误:', err);
            wsConnected = false;
        };

        ws.onclose = function () {
            logWarn('WebSocket 连接关闭');
            wsConnected = false;
            ws = null;
            scheduleReconnect();
        };
    }

    function scheduleReconnect() {
        if (!isRunning) return;
        if (wsReconnectTimer) clearTimeout(wsReconnectTimer);
        if (wsReconnectAttempts >= WS_MAX_RECONNECT_ATTEMPTS) {
            logError('WebSocket 重连次数已达上限，停止尝试');
            return;
        }
        wsReconnectAttempts++;
        const delay = WS_RECONNECT_DELAY * Math.min(wsReconnectAttempts, 5);
        logWarn(`将在 ${delay}ms 后尝试重连 (第 ${wsReconnectAttempts} 次)`);
        wsReconnectTimer = setTimeout(() => {
            wsReconnectTimer = null;
            connectWebSocket();
        }, delay);
    }

    function sendWebSocketRequest(payload) {
        return new Promise((resolve, reject) => {
            if (!wsConnected || !ws || ws.readyState !== WebSocket.OPEN) {
                reject(new Error('WebSocket 未连接'));
                return;
            }

            const id = ++wsRequestId;
            const message = Object.assign({ id: id }, payload);

            const timeout = setTimeout(() => {
                pendingRequests.delete(id);
                reject(new Error('WebSocket 请求超时'));
            }, 30000);

            pendingRequests.set(id, { resolve, reject, timer: timeout });

            try {
                ws.send(JSON.stringify(message));
                logInfo(`🚀 WebSocket 发送: ${payload.action} ${payload.path || ''}`);
            } catch (e) {
                clearTimeout(timeout);
                pendingRequests.delete(id);
                reject(e);
            }
        });
    }

    // ============ 通过 WebSocket 获取提示词 ============
    function getPromptViaWebSocket() {
        return new Promise((resolve, reject) => {
            if (!wsConnected || !ws || ws.readyState !== WebSocket.OPEN) {
                connectWebSocket();
                const startWait = Date.now();
                const waitForConnection = () => {
                    if (wsConnected && ws && ws.readyState === WebSocket.OPEN) {
                        sendWebSocketRequest({ action: 'prompt' })
                            .then(result => {
                                if (result.status === 'success' && result.data && result.data.prompt) {
                                    resolve(result.data.prompt);
                                } else {
                                    reject(new Error(result.message || '获取提示词失败'));
                                }
                            })
                            .catch(reject);
                    } else if (Date.now() - startWait > 5000) {
                        reject(new Error('WebSocket 连接超时，无法获取提示词'));
                    } else {
                        setTimeout(waitForConnection, 200);
                    }
                };
                waitForConnection();
            } else {
                sendWebSocketRequest({ action: 'prompt' })
                    .then(result => {
                        if (result.status === 'success' && result.data && result.data.prompt) {
                            resolve(result.data.prompt);
                        } else {
                            reject(new Error(result.message || '获取提示词失败'));
                        }
                    })
                    .catch(reject);
            }
        });
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

        if (!wsConnected) {
            connectWebSocket();
        }

        const startWait = Date.now();
        const waitForConnection = () => {
            if (wsConnected && ws && ws.readyState === WebSocket.OPEN) {
                sendWebSocketRequest(payload)
                    .then(result => {
                        logInfo('✅ 操作结果:', result);
                        callback(null, result);
                    })
                    .catch(err => {
                        logError('WebSocket 请求失败:', err);
                        callback(err, null);
                    });
            } else if (Date.now() - startWait > 5000) {
                logError('WebSocket 连接超时');
                callback(new Error('WebSocket 连接超时'), null);
            } else {
                setTimeout(waitForConnection, 200);
            }
        };
        waitForConnection();
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

        if (!wsConnected) {
            connectWebSocket();
        }

        if (!skipPrompt && !(location.pathname || '').length < 5) {
            getPromptViaWebSocket()
                .then(prompt => {
                    logInfo('获取到服务端提示词（通过 WebSocket）');
                    sendPromptOnce(prompt);
                })
                .catch(err => {
                    logWarn('获取提示词失败:', err);
                    const useDefault = confirm(
                        '获取服务端提示词失败，是否使用内置默认提示词继续？\n\n' +
                        '错误信息：' + (err.message || '未知错误')
                    );
                    if (useDefault) {
                        sendPromptOnce(DEFAULT_PROMPT);
                    } else {
                        stopAgent();
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
        if (wsReconnectTimer) {
            clearTimeout(wsReconnectTimer);
            wsReconnectTimer = null;
        }
        if (ws) {
            try {
                ws.close();
            } catch (e) {}
            ws = null;
        }
        wsConnected = false;
        for (const [id, pending] of pendingRequests) {
            clearTimeout(pending.timer);
            pending.reject(new Error('Agent 已停止'));
        }
        pendingRequests.clear();
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

        // ========== 重置按钮点击事件（透明背景模态框） ==========
        resetBtn.addEventListener('click', function (e) {
            e.stopPropagation();

            const modal = document.createElement('div');
            modal.style.cssText = `position: fixed;bottom: 20%;left: 50%;transform: translateX(-50%);background: transparent;padding: 0;z-index: 10001;display: flex;flex-direction: column;gap: 8px;align-items: center;color: #222;cursor: move;`;

            const input = document.createElement('textarea');
            input.placeholder = '输入要发送的内容... (Ctrl+Enter 发送)';
            input.style.cssText = `background: white;width: 400px;max-width: 90vw;height: 120px;padding: 12px 14px;font-size: 14px;border-radius: 8px;border: 1px solid #ccc;resize: none;color: #222;outline: none;box-shadow: 0 2px 10px rgba(0,0,0,0.15);box-sizing: border-box;`;

            const btnContainer = document.createElement('div');
            btnContainer.style.cssText = 'display: flex; gap: 10px; justify-content: flex-end; align-items: center; width: 100%;';

            const dragHint = document.createElement('span');
            dragHint.textContent = '⬇️ 拖动移动';
            dragHint.style.cssText = 'color: #555; font-size: 13px; margin-right: auto; cursor: move; background: rgba(255,255,255,0.8); padding: 2px 8px; border-radius: 12px;';

            const sendBtn = document.createElement('button');
            sendBtn.textContent = '发送 (Ctrl+Enter)';
            sendBtn.style.cssText = `padding: 8px 20px;cursor: pointer;border: none;border-radius: 8px;background: #3964fe;color: white;font-size: 14px;font-weight: 500;transition: background 0.2s;box-shadow: 0 2px 6px rgba(0,0,0,0.1);`;
            sendBtn.addEventListener('mouseenter', () => sendBtn.style.background = '#2b4fc7');
            sendBtn.addEventListener('mouseleave', () => sendBtn.style.background = '#3964fe');

            const closeBtn = document.createElement('button');
            closeBtn.textContent = '取消';
            closeBtn.style.cssText = `padding: 8px 20px;cursor: pointer;border: none;border-radius: 8px;background: #6b7280;color: white;font-size: 14px;transition: background 0.2s;box-shadow: 0 2px 6px rgba(0,0,0,0.1);`;
            closeBtn.addEventListener('mouseenter', () => closeBtn.style.background = '#4b5563');
            closeBtn.addEventListener('mouseleave', () => closeBtn.style.background = '#6b7280');

            btnContainer.appendChild(dragHint);
            btnContainer.appendChild(sendBtn);
            btnContainer.appendChild(closeBtn);

            modal.appendChild(input);
            modal.appendChild(btnContainer);

            function closeModal() {
                modal.remove();
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
            }

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
                    input.focus();
                }
            }

            let isDragging = false;
            let startX, startY, origLeft, origTop;

            function onMouseMove(e) {
                if (!isDragging) return;
                const dx = e.clientX - startX;
                const dy = e.clientY - startY;
                modal.style.left = (origLeft + dx) + 'px';
                modal.style.top = (origTop + dy) + 'px';
            }

            function onMouseUp() {
                isDragging = false;
            }

            modal.addEventListener('mousedown', function (e) {
                const target = e.target;
                if (target.closest('textarea') || target.closest('button')) {
                    return;
                }
                isDragging = true;
                startX = e.clientX;
                startY = e.clientY;
                const rect = modal.getBoundingClientRect();
                modal.style.left = rect.left + 'px';
                modal.style.top = rect.top + 'px';
                modal.style.transform = 'none';
                origLeft = rect.left;
                origTop = rect.top;
                e.preventDefault();
            });

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);

            input.addEventListener('keydown', function(e) {
                if (e.ctrlKey && e.key === 'Enter') {
                    e.preventDefault();
                    sendText();
                }
            });

            sendBtn.addEventListener('click', sendText);
            closeBtn.addEventListener('click', closeModal);

            document.body.appendChild(modal);
            input.focus();
        });

        container.appendChild(btn);
        container.appendChild(resetBtn);

        let isDraggingContainer = false;
        let startXc, startYc, origXc, origYc;

        container.addEventListener('mousedown', function (e) {
            if (e.target === container) {
                isDraggingContainer = true;
                startXc = e.clientX;
                startYc = e.clientY;
                const rect = container.getBoundingClientRect();
                origXc = rect.left;
                origYc = rect.top;
                e.preventDefault();
            }
        });

        document.addEventListener('mousemove', function (e) {
            if (!isDraggingContainer) return;
            const dx = e.clientX - startXc;
            const dy = e.clientY - startYc;
            container.style.left = (origXc + dx) + 'px';
            container.style.top = (origYc + dy) + 'px';
            container.style.right = 'auto';
            container.style.bottom = 'auto';
        });

        document.addEventListener('mouseup', function () {
            isDraggingContainer = false;
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