#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# file name is agent_executor.py

import os
import json
import logging
import asyncio
import requests
from pathlib import Path
from logging.handlers import RotatingFileHandler
import threading
import time
from toolUtil import TOOLS, execute_tool, CFG

try:
    import websockets
except ImportError:
    websockets = None

# ---------- 配置（支持环境变量） ----------
WORK_DIR_ENV = os.getenv("AGENT_WORK_DIR", "/work")
WS_HOST = os.getenv("AGENT_WS_HOST", "127.0.0.1")
WS_PORT = int(os.getenv("AGENT_WS_PORT", 8765))

LLAMA_HOST = os.getenv("AGENT_LLAMA_HOST", "http://127.0.0.1:9931")
LLAMA_API_KEY = os.getenv("AGENT_LLAMA_API_KEY", "sk-no-key-required")
LLAMA_MODEL = os.getenv("AGENT_LLAMA_MODEL", "local-model")
LLAMA_TIMEOUT = int(os.getenv("AGENT_LLAMA_TIMEOUT", 600))

# ---------- 工作目录 ----------
AGENT_DIR = Path(__file__).parent
WORK_DIR = Path(WORK_DIR_ENV).resolve()
WORK_DIR.mkdir(exist_ok=True, parents=True)

# ---------- 日志配置（轮转） ----------
LOG_FILE = AGENT_DIR / "execution.log"
logger = logging.getLogger('AgentExecutor')
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logger.addHandler(console)

# ---------- 配置（已去掉沙箱约束） ----------
CFG.root = Path(WORK_DIR).resolve()
CFG.root.mkdir(parents=True, exist_ok=True)
CFG.read_only = False
CFG.allow_shell = True
CFG.allow_python = True
CFG.allow_network = True
CFG.shell_whitelist = []          # 空 = 不做命令白名单检查
CFG.allowed_hosts = None          # None = 网络全部放行
CFG.audit_log = CFG.root / "audit.log"


def load_web_prompt():
    """读取 prompt.md 作为系统提示词，供客户端 AI 使用。"""
    prompt_file = AGENT_DIR / "prompt.md"
    if prompt_file.exists():
        text = prompt_file.read_text(encoding='utf-8').strip()
        if text:
            return text
    return ""


def load_local_system_prompt():
    """给本地 LLM 用的 system prompt。优先读 prompt_local_llm.md，否则用内置。"""
    f = AGENT_DIR / "prompt_local_llm.md"
    if f.exists():
        text = f.read_text(encoding='utf-8').strip()
        if text:
            return text
    return ""


# ---------- 响应封装 ----------
def make_response(status, data=None, message=None):
    resp = {"status": status}
    if data is not None:
        resp["data"] = data
    if message is not None:
        resp["message"] = message
    return resp


# ---------- 模型调用 ----------
def chat_completion(messages,
                    tools=None,
                    host=None,
                    api_key=None,
                    model=None,
                    temperature=0.7,
                    stream=False,
                    timeout=None):
    host = host or LLAMA_HOST
    api_key = api_key or LLAMA_API_KEY
    model = model or LLAMA_MODEL
    timeout = timeout or LLAMA_TIMEOUT

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": bool(stream),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    url = f"{host}/v1/chat/completions"
    max_attempts = 3
    r = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning(f"LLM request failed (attempt {attempt}/{max_attempts}): {e}")
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))
                continue
            raise RuntimeError(f"LLM connection failed after {max_attempts} attempts: {e}")

        if r.status_code >= 500:
            logger.warning(f"LLM returned {r.status_code} (attempt {attempt}/{max_attempts})")
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))
                continue
            raise RuntimeError(f"LLM {r.status_code}: {r.text[:500]}")

        if r.status_code >= 400:
            body = r.text[:2000]
            logger.error(f"LLM returned {r.status_code}: {body}")
            raise RuntimeError(f"LLM {r.status_code}: {body[:500]}")

        break

    if stream:
        return r
    return r.json()


# ---------- Agent 循环 ----------
def run_agent_once(chat_completion_fn, messages):
    """
    单轮工具模式：
      1. 调模型，拿到 tool_calls
      2. 执行所有工具
      3. 直接返回工具执行结果，不再回传给模型
      4. 若模型没有请求工具，则直接返回它的文本回复
    """
    resp = chat_completion_fn(
        messages=messages,
        tools=TOOLS,
        stream=False,
    )

    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        logger.error(f"模型响应结构异常: {e}, resp={resp}")
        raise RuntimeError(f"模型响应结构异常: {e}")

    content_ = resp["choices"][0]["message"]["content"]
    logger.info(f"🤖 : {content_}")

    tool_calls = msg.get("tool_calls") or []

    # ---- 情况 A：模型没有请求工具，直接返回文本 ----
    if not tool_calls:
        content = msg.get("content") or ""
        logger.info(f"模型未请求工具，直接返回文本 {len(content)} 字符")
        return {
            "type": "text",
            "content": content,
        }

    # ---- 情况 B：模型请求了工具，逐个执行，收集结果 ----
    logger.info(f"模型请求 {len(tool_calls)} 个工具调用，开始执行")

    results = []
    for idx, tc in enumerate(tool_calls):
        tc_id = tc.get("id") or f"call_{idx}"
        fn = tc.get("function") or {}
        name = fn.get("name")
        args = fn.get("arguments")

        logger.info(f"  → 执行工具 {name}, 参数 {str(args)[:200]}")

        try:
            result = execute_tool(name, args)
        except Exception as e:
            logger.error(f"工具 {name} 执行失败: {e}", exc_info=True)
            result = {"error": str(e)}

        results.append(result)

    logger.info(f"工具全部执行完毕，共 {len(results)} 个")

    return {
        "type": "tools",
        "content": content_ + "\n".join(str(r) for r in results),
    }


# ---------- 核心处理：一条客户端消息 ----------
async def handle_client_message(data, request_id):
    """
    处理来自 WebSocket 客户端的一条消息。
    返回 response dict（不含 id，调用方补）。
    """
    # ---- action == 'prompt' ----
    if data.get('action') == 'prompt':
        try:
            prompt = load_web_prompt()
            return make_response("success", data={"prompt": prompt})
        except Exception as e:
            logger.error(f"获取提示词失败: {e}")
            return make_response("error", message=str(e))

    # ---- 其他未知 action ----
    if 'action' in data:
        return make_response("error", message=f"不支持的 action: {data.get('action')}")

    # ---- 无 action：视为客户端回传的 AI 回复 ----
    content = data.get('content')
    if content is None:
        return make_response("error", message="缺少 content 字段")
    if not isinstance(content, str):
        return make_response("error", message=f"content 字段类型错误: {type(content).__name__}")
    if not content.strip():
        return make_response("skipped", message="content 为空，已跳过")

    logger.info(f"消息 {content}")

    if "@小牛" not in content:
        return make_response("skipped", message="非小牛的消息")

    # ---- 组装 messages 并跑 Agent ----
    try:
        system_prompt = load_local_system_prompt()
    except Exception as e:
        logger.warning(f"加载 prompt 失败，使用默认: {e}")
        system_prompt = ""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]

    logger.info(f"开始 Agent 循环，消息 {len(content)} 字符")
    logger.info(f"😊 : {messages[1]['content']}")
    try:
        outcome = await asyncio.to_thread(run_agent_once, chat_completion, messages)

        if outcome["type"] == "text":
            content_ = outcome["content"]
            if "不关我事" in content_:
                return make_response("success", data="")
            return make_response("success", data="小牛：" + content_)

        return make_response("success", data="小牛：" + outcome["content"])

    except Exception as e:
        logger.error(f"Agent 执行异常: {e}", exc_info=True)
        return make_response("error", message=f"Agent 执行失败: {e}")


# ---------- WebSocket 处理 ----------
async def websocket_handler(websocket):
    peer = getattr(websocket, "remote_address", None)
    logger.info(f"✅ 客户端已连接: {peer}")
    try:
        async for message in websocket:
            # 二进制帧兼容
            if isinstance(message, bytes):
                try:
                    message = message.decode('utf-8')
                except UnicodeDecodeError:
                    logger.error(f"无法用 UTF-8 解码二进制数据: {message!r}")
                    await websocket.send(json.dumps(
                        make_response("error", message="无法解码二进制消息"),
                        ensure_ascii=False))
                    continue

            # 解析 JSON
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                await websocket.send(json.dumps(
                    make_response("error", message="无效的 JSON"),
                    ensure_ascii=False))
                continue

            if not isinstance(data, dict):
                await websocket.send(json.dumps(
                    make_response("error", message="JSON 消息必须是对象"),
                    ensure_ascii=False))
                continue

            request_id = data.get('id')

            try:
                result = await handle_client_message(data, request_id)
            except Exception as e:
                logger.error(f"处理单条消息异常: {e}", exc_info=True)
                result = make_response("error", message=f"内部错误: {e}")

            if result is None:
                continue
            if request_id is not None:
                result['id'] = request_id

            try:
                await websocket.send(json.dumps(result, ensure_ascii=False))
            except Exception as e:
                logger.error(f"发送响应失败: {e}")

    except websockets.exceptions.ConnectionClosed:
        logger.info("WebSocket 连接关闭")
    except Exception as e:
        logger.error(f"WebSocket 处理异常: {e}", exc_info=True)


# ---------- 启动 WebSocket 服务 ----------
if __name__ == '__main__':
    if websockets is None:
        print("❌ 未安装 websockets 库，请执行: pip install websockets")
        exit(1)

    print("🚀 Agent WebSocket 服务启动中")
    print(f"📁 工作目录 (默认相对根): {WORK_DIR}")
    print(f"🔌 WebSocket 监听地址: {WS_HOST}:{WS_PORT}")
    print(f"🧠 模型接口: {LLAMA_HOST}  model={LLAMA_MODEL}")
    print(f"📋 日志文件: {LOG_FILE} (轮转: 10MB/5备份)")
    web_prompt_file = AGENT_DIR / "prompt.md"
    local_prompt_file = AGENT_DIR / "prompt_local_llm.md"
    print(f"📝 网页 AI 提示词: {web_prompt_file if web_prompt_file.exists() else '内置默认'}")
    print(f"🧠 本地 LLM 提示词: {local_prompt_file if local_prompt_file.exists() else '内置默认'}")
    print(f"🧰 沙箱约束: 已全部解除（路径/命令/网络无限制）")


    async def start_ws():
        async with websockets.serve(websocket_handler, WS_HOST, WS_PORT):
            await asyncio.Future()  # 永久运行

    print("🚀 Agent WebSocket 服务启动完毕")

    try:
        asyncio.run(start_ws())
    except KeyboardInterrupt:
        print("服务已停止")