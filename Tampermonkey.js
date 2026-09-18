// ==UserScript==
// @name         DeepSeek Agent (基于 WebSocket)
// @namespace    http://tampermonkey.net/
// @version      10.1
// @description  无遮罩、无标题、透明背景模态框，拖动整个空白区域；全部通信走 WebSocket，获取提示词失败弹框确认
// @author       小马
// @match        https://chat.deepseek.com/*
// @grant        none
// ==/UserScript==

(function () {
    'use strict';

    const DEFAULT_PROMPT = `你好`;
    // ============ 配置 ============
    const WS_URL = 'ws://localhost:8765';
    // ============ 状态 ============
    let isRunning = false;
    const SILENT_WAIT = 1000;   // 静默时间（ms）
    const SILENT_TEXT_ROUND = 5;   // 静默轮次
    let processing = false;      // 是否正在执行任务
    let iteration = 0;           // 已执行步数
    // WebSocket 相关
    let ws = null;
    let wsConnected = false;
    let wsRequestId = 0;
    const pendingRequests = new Map();
    let wsReconnectTimer = null;

    // ============ 日志工具 ============
    function logInfo(...args) {
        console.log('[Agent]', ...args);
    }

    function logWarn(...args) {
        console.warn('[Agent]', ...args);
    }

    function logError(...args) {
        console.error('[Agent]', ...args);
    }

    // DOM 观察器
    let observerTimer = null;

    // ============ 通过虚拟列表 key 获取最新 AI 消息 ============
    function getLatestAIMessage() {
        const items = document.querySelectorAll('[data-virtual-list-item-key]');
        if (!items.length) return null;

        let item = items[items.length - 1];
        let key = item.dataset.virtualListItemKey;
        const latest = key % 2 === 0 ? item : null;
        if (!latest) return null;
        let childNode = latest.childNodes[0]?.childNodes[1];
        const text = childNode?.innerText?.trim()
        return {text, childNode, key};
    }

    top.window.debugAgent = getLatestAIMessage;

    function sendMessage(text) {
        logInfo('📤 发送消息:', text.slice(0, 200) + (text.length > 200 ? '...' : ''));
        const ta = document.querySelector('textarea[placeholder*="发送消息"]');
        if (!ta) {
            logError('未找到输入框');
            return;
        }
        ta.focus();

        let success = false;
        if (document.execCommand) {
            try {
                success = document.execCommand('insertText', false, text);
            } catch (e) {
                // ignore
            }
        }
        if (!success) {
            ta.value = text;
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            ta.dispatchEvent(new Event('change', {bubbles: true}));
            ta.dispatchEvent(new Event('compositionend', {bubbles: true}));
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
                        ta.dispatchEvent(new Event('input', {bubbles: true}));
                    }
                }, 100);
            } else {
                ta.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
                ta.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', bubbles: true}));
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
            logInfo('✅ WebSocket 已连接');
            // 重连后旧的 pending 请求全部 reject，避免内存泄漏和永久 pending
            const ids = Array.from(pendingRequests.keys());
            for (const id of ids) {
                const pending = pendingRequests.get(id);
                if (!pending) continue;
                clearTimeout(pending.timer);
                try {
                    pending.reject(new Error('WebSocket 重连，请求已取消'));
                } catch (e) {
                    // ignore
                }
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
            if (requestId != null && pendingRequests.has(requestId)) {
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
        wsReconnectTimer = setTimeout(() => {
            wsReconnectTimer = null;
            connectWebSocket();
        }, 1000);
    }

    function sendWebSocketRequest(payload) {
        return new Promise((resolve, reject) => {
            if (!wsConnected || !ws || ws.readyState !== WebSocket.OPEN) {
                reject(new Error('WebSocket 未连接'));
                return;
            }

            const id = ++wsRequestId;
            const message = Object.assign({id}, payload);

            const timeout = setTimeout(() => {
                pendingRequests.delete(id);
                reject(new Error('WebSocket 请求超时'));
            }, 10 * 60 * 1000);

            pendingRequests.set(id, {resolve, reject, timer: timeout});

            try {
                ws.send(JSON.stringify(message));
                logInfo(`🚀 WebSocket 发送: ${JSON.stringify(payload).slice(0, 100)}`);
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
            const doRequest = () => {
                sendWebSocketRequest({action: 'prompt'})
                    .then(result => {
                        if (result.status === 'success' && result.data && result.data.prompt) {
                            resolve(result.data.prompt);
                        } else {
                            reject(new Error(result.message || '获取提示词失败'));
                        }
                    })
                    .catch(reject);
            };

            if (!wsConnected || !ws || ws.readyState !== WebSocket.OPEN) {
                connectWebSocket();
                const startWait = Date.now();
                const waitForConnection = () => {
                    if (wsConnected && ws && ws.readyState === WebSocket.OPEN) {
                        doRequest();
                    } else if (Date.now() - startWait > 5000) {
                        reject(new Error('WebSocket 连接超时，无法获取提示词'));
                    } else {
                        setTimeout(waitForConnection, 200);
                    }
                };
                waitForConnection();
            } else {
                doRequest();
            }
        });
    }

    // ============ 监听 AI 回复 ============
    function stopDOMObserver() {
        if (observerTimer) {
            clearInterval(observerTimer);
            observerTimer = null;
            logInfo('监听 AI 回复已停止');
        }
    }

    function setupDOMObserver() {
        stopDOMObserver();

        let lastAIMessageKey = -1;   // 上一次处理的 key
        let lastProcessedText = '';  // 上一次文本
        let textRound = 1;  // 上一次文本
        let lastOver = false;        // 该条消息是否已处理完

        observerTimer = setInterval(() => {
            const latest = getLatestAIMessage();
            if (!latest) return;

            // 出现新消息 → 重置状态
            if (latest.key !== lastAIMessageKey) {
                lastAIMessageKey = latest.key;
                lastProcessedText = latest.text || '';
                lastOver = false;
                logInfo(lastAIMessageKey, `检测到新的 AI 消息 key=${latest.key}`, `${lastProcessedText.slice(0, 20)}...${lastProcessedText.slice(-20)}`);
                return;
            }

            const finalText = latest.text || '';

            // 文本还在增长
            if (finalText !== lastProcessedText) {
                lastProcessedText = finalText;
                textRound = 0;
                lastOver = false;
                return;
            }
            if (lastOver) {
                logInfo(lastAIMessageKey, `文本稳定 → 判定回复完毕`)
                return;
            }

            textRound++;
            if (textRound < SILENT_TEXT_ROUND) {
                logInfo(lastAIMessageKey, `轮次未到，继续等待 ${textRound}/${SILENT_TEXT_ROUND}`)
            }

            if (!isRunning || processing) {
                logInfo(lastAIMessageKey, `运行状态：${isRunning}`, `任务状态：${isRunning}`)
                return;
            }

            lastOver = true;
            processing = true;
            iteration++;

            logInfo(lastAIMessageKey, 'AI 回复内容预览:', `(长度: ${finalText.length})`, `${lastProcessedText.slice(0, 20)}...${lastProcessedText.slice(-20)}`);

            if (!wsConnected) connectWebSocket();

            sendWebSocketRequest({content: finalText})
                .then(result => {
                    logInfo(lastAIMessageKey, '后端处理结果:', result);

                    // 后端判定为非操作类，跳过：不回灌消息，避免死循环
                    if (result.status === 'skipped') {
                        logInfo(lastAIMessageKey, '后端跳过该消息:', result.message || '');
                        processing = false;
                        return;
                    }

                    let msg;
                    if (result.status === 'success') {
                        const data = result.data;
                        let dataStr;
                        if (data === null || data === undefined) {
                            dataStr = '无返回数据';
                        } else if (typeof data === 'object') {
                            dataStr = JSON.stringify(data, null, 2);
                        } else {
                            dataStr = String(data);
                        }
                        const MAX_LEN = 8000;
                        if (dataStr.length > MAX_LEN) {
                            dataStr = dataStr.slice(0, MAX_LEN)
                                + `\n...（已截断，原始长度 ${dataStr.length} 字符）`;
                        }
                        msg = '执行结果：' + dataStr;
                    } else {
                        msg = `执行结果：操作失败，${result.message || '未知错误'}`;
                        // 出错时允许重新处理同一 AI 消息
                        lastOver = false;
                    }
                    sendMessage(msg);
                    processing = false;
                })
                .catch(err => {
                    logError('后端处理请求失败:', err);
                    lastOver = false;
                    processing = false;
                });
        }, SILENT_WAIT);

        logInfo(lastAIMessageKey, `监听 AI 回复已启动（周期 ${SILENT_WAIT}ms）`);
    }

    function sendPromptOnce(prompt) {
        const pathname = location.pathname.length > 10 ? location.pathname : Math.random().toString();
        if (!localStorage.getItem('agent_prompt_sent' + pathname)) {
            localStorage.setItem('agent_prompt_sent' + pathname, 'true');
            setTimeout(() => {
                sendMessage(prompt);
                logInfo('系统提示词已发送');
            }, 1500);
        }
    }

    function startAgent(skipPrompt) {
        logInfo('启动 Agent');
        iteration = 0;
        processing = false;

        if (!wsConnected) connectWebSocket();

        if (!skipPrompt && !(location.pathname || '').length < 5) {
            getPromptViaWebSocket()
                .then(prompt => {
                    logInfo('获取到服务端提示词');
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

        stopDOMObserver();

        if (wsReconnectTimer) {
            clearTimeout(wsReconnectTimer);
            wsReconnectTimer = null;
        }
        if (ws) {
            try {
                ws.close();
            } catch (e) {
                // ignore
            }
            ws = null;
        }
        wsConnected = false;

        for (const [id, pending] of pendingRequests) {
            clearTimeout(pending.timer);
            try {
                pending.reject(new Error('Agent 已停止'));
            } catch (e) {
                // ignore
            }
        }
        pendingRequests.clear();

        const btn = document.querySelector('.agent-toggle-btn');
        if (btn) {
            const span = btn.querySelector('span');
            if (span) span.textContent = 'Agent';
            btn.style.backgroundColor = '';
        }
        logInfo('已停止');
    }

    // ============ 悬浮按钮容器 ============
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
        resetBtn.innerHTML = `<span style="font-size:13px;">📝 重置</span>`;

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
                if (target.closest('textarea') || target.closest('button')) return;
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

            input.addEventListener('keydown', function (e) {
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
