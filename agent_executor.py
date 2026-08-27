#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import logging
import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from logging.handlers import RotatingFileHandler

try:
    import websockets
except ImportError:
    websockets = None

# ---------- 配置（支持环境变量） ----------
WORK_DIR_ENV = os.getenv("AGENT_WORK_DIR", None)
WS_HOST = os.getenv("AGENT_WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("AGENT_WS_PORT", 8765))

# ---------- 工作目录 ----------
AGENT_DIR = Path(__file__).parent
if WORK_DIR_ENV:
    WORK_DIR = Path(WORK_DIR_ENV).resolve()
else:
    WORK_DIR = AGENT_DIR / "agent_workspace"
WORK_DIR.mkdir(exist_ok=True)

# ---------- 日志配置（轮转） ----------
LOG_FILE = AGENT_DIR / "execution.log"
logger = logging.getLogger('AgentExecutor')
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logger.addHandler(console)

# ---------- 提示词缓存 ----------
DEFAULT_PROMPT = "你好"
_prompt_cache = {"prompt": DEFAULT_PROMPT, "mtime": None}

def load_prompt():
    prompt_file = AGENT_DIR / "prompt.md"
    if prompt_file.exists():
        mtime = prompt_file.stat().st_mtime
        if _prompt_cache["mtime"] != mtime:
            _prompt_cache["prompt"] = prompt_file.read_text(encoding='utf-8')
            _prompt_cache["mtime"] = mtime
        return _prompt_cache["prompt"]
    return DEFAULT_PROMPT

# ---------- 辅助函数 ----------
def safe_path(file_path):
    """防止路径遍历攻击"""
    full_path = (WORK_DIR / file_path).resolve()
    real_work = WORK_DIR.resolve()
    if str(full_path) != str(real_work) and not str(full_path).startswith(str(real_work) + os.sep):
        raise ValueError("非法路径")
    return full_path

def make_response(status, data=None, message=None):
    resp = {"status": status}
    if data is not None:
        resp["data"] = data
    if message is not None:
        resp["message"] = message
    return resp
def extract_command_from_text(text):
    """
    从 AI 响应文本中提取工具调用 JSON。
    优先匹配 ```json ... ``` 代码块，否则尝试将整个文本解析为 JSON。
    返回解析后的 dict，若失败返回 None。
    """
    import re
    # 匹配 ```json ... ```
    match = re.search(r'```json\s*([\s\S]*?)\s*```', text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试直接解析整个文本
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None

def process_ai_response(data, request_id=None):
    """
    处理 AI 响应：提取命令并执行。
    data 应包含 'content' 字段（AI 返回的完整文本），
    或者整个 data 本身就是待解析的文本（当消息直接为字符串时，但 WebSocket 要求 JSON，所以通常会有 content）。
    """
    content = data.get('content')
    if not content:
        # 如果 data 直接就是文本，但这里 data 是 dict，所以必须有 content
        return make_response("error", message="缺少 content 字段")
    if content.startswith("🤖"):
        return
    content = content.removeprefix("json复制下载")
    logger.info(f"解码后字符串: {content}")
    cmd = extract_command_from_text(content)
    if not cmd:
        return make_response("error", message="无法从 AI 响应中解析出工具调用 JSON")

    # 构建标准动作数据（兼容原有字段名）
    # 如果 cmd 中已经含有 action, path 等，直接使用；否则尝试从 type 推断
    if 'action' not in cmd:
        # 若 type 为 'file'，则使用 cmd 中的 action（如果有）
        if cmd.get('type') == 'file' and 'action' in cmd:
            # 已经包含 action
            pass
        else:
            return make_response("error", message="解析出的命令缺少 action 字段")

    # 现在 cmd 应包含 action、path 等，直接调用执行函数
    return execute_action(cmd, request_id)
# ---------- 文件操作实现 ----------
def handle_list(data):
    offset = max(0, int(data.get('offset', 0)))
    limit = max(0, int(data.get('limit', 0)))   # 0 表示不限制
    files = []
    for root, dirs, filenames in os.walk(WORK_DIR):
        for f in filenames:
            full = Path(root) / f
            rel = full.relative_to(WORK_DIR)
            files.append({"path": str(rel), "size": full.stat().st_size})
    files.sort(key=lambda x: x['path'])
    total = len(files)
    if limit > 0:
        files = files[offset:offset+limit]
    else:
        files = files[offset:]
    return make_response("success", data={"files": files, "total": total, "offset": offset, "limit": limit})

def handle_create(full_path, content):
    full_path.parent.mkdir(parents=True, exist_ok=True)
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(content)
    logger.info(f"创建文件: {full_path.relative_to(WORK_DIR)}")
    return make_response("success", data={"path": str(full_path.relative_to(WORK_DIR)), "size": full_path.stat().st_size})

def handle_read(full_path, offset=0, limit=None, tail=False):
    if not full_path.exists():
        raise FileNotFoundError(f"文件不存在: {full_path.relative_to(WORK_DIR)}")
    if full_path.is_dir():
        raise IsADirectoryError(f"路径是目录，不能读取: {full_path.relative_to(WORK_DIR)}")

    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
    if full_path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError(f"文件过大，拒绝读取（最大允许 {MAX_FILE_SIZE // (1024*1024)}MB）: {full_path.relative_to(WORK_DIR)}")

    with open(full_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    total_lines = len(lines)

    if tail:
        if limit is None:
            limit = 10
        start = max(0, total_lines - limit)
        selected = lines[start:]
    else:
        start = max(0, offset)
        if limit is not None and limit > 0:
            end = min(start + limit, total_lines)
            selected = lines[start:end]
        else:
            selected = lines[start:]

    selected_text = ''.join(selected)
    return make_response("success", data={
        "path": str(full_path.relative_to(WORK_DIR)),
        "content": selected_text,
        "total_lines": total_lines,
        "returned_lines": len(selected),
        "offset": start,
        "limit": limit,
        "tail": tail
    })

def handle_overwrite(full_path, content):
    if not full_path.exists():
        raise FileNotFoundError(f'文件不存在: {full_path.relative_to(WORK_DIR)}')
    if full_path.is_dir():
        raise IsADirectoryError(f'路径是目录，不能执行此操作: {full_path.relative_to(WORK_DIR)}')
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(content or '')
    logger.info(f"覆盖文件: {full_path.relative_to(WORK_DIR)}")
    return make_response("success", data={"path": str(full_path.relative_to(WORK_DIR)), "size": full_path.stat().st_size})

def handle_append(full_path, content):
    if not full_path.exists():
        raise FileNotFoundError(f'文件不存在: {full_path.relative_to(WORK_DIR)}')
    if full_path.is_dir():
        raise IsADirectoryError(f'路径是目录，不能执行此操作: {full_path.relative_to(WORK_DIR)}')
    with open(full_path, 'a', encoding='utf-8') as f:
        f.write(content or '')
    logger.info(f"追加文件: {full_path.relative_to(WORK_DIR)}")
    return make_response("success", data={"path": str(full_path.relative_to(WORK_DIR)), "size": full_path.stat().st_size})

def handle_replace(full_path, search, replace, count=1):
    if not full_path.exists():
        raise FileNotFoundError(f'文件不存在: {full_path.relative_to(WORK_DIR)}')
    if full_path.is_dir():
        raise IsADirectoryError(f'路径是目录，不能执行此操作: {full_path.relative_to(WORK_DIR)}')

    if count == 0:
        logger.info(f"替换操作跳过（count=0）: {full_path.relative_to(WORK_DIR)}")
        return make_response("success", data={
            "path": str(full_path.relative_to(WORK_DIR)),
            "size": full_path.stat().st_size,
            "replaced_count": 0,
            "search": search,
            "replace": replace,
            "count": count
        })

    pattern = re.compile(re.escape(search))
    replaced_count = 0
    remaining = None if count == -1 else count

    tmp_fd, tmp_path = tempfile.mkstemp(dir=full_path.parent, prefix='.replace_tmp_')
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as tmp_file:
            with open(full_path, 'r', encoding='utf-8') as src_file:
                for line in src_file:
                    if remaining is None:
                        new_line, sub_count = pattern.subn(replace, line)
                        replaced_count += sub_count
                    elif remaining > 0:
                        new_line, sub_count = pattern.subn(replace, line, count=remaining)
                        replaced_count += sub_count
                        remaining -= sub_count
                    else:
                        new_line = line
                    tmp_file.write(new_line)
        os.replace(tmp_path, full_path)
        logger.info(f"替换文件: {full_path.relative_to(WORK_DIR)}，替换次数: {replaced_count}")
        return make_response("success", data={
            "path": str(full_path.relative_to(WORK_DIR)),
            "size": full_path.stat().st_size,
            "replaced_count": replaced_count,
            "search": search,
            "replace": replace,
            "count": count
        })
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

def handle_delete(full_path, recursive=False):
    if not full_path.exists():
        raise FileNotFoundError(f"文件不存在: {full_path.relative_to(WORK_DIR)}")
    if full_path.is_dir():
        if recursive:
            for root, dirs, files in os.walk(full_path):
                for name in dirs + files:
                    p = Path(root) / name
                    if p.is_symlink():
                        raise ValueError(f'目录内包含符号链接，禁止递归删除: {p.relative_to(WORK_DIR)}')
            shutil.rmtree(full_path)
            logger.info(f"递归删除目录: {full_path.relative_to(WORK_DIR)}")
            return make_response("success", data={"path": str(full_path.relative_to(WORK_DIR)), "deleted": True, "recursive": True})
        else:
            raise IsADirectoryError(f"路径是目录，不允许删除（设置 recursive=true 可强制删除）: {full_path.relative_to(WORK_DIR)}")
    else:
        if full_path.is_symlink():
            raise ValueError('不允许删除符号链接')
        full_path.unlink()
        logger.info(f"删除文件: {full_path.relative_to(WORK_DIR)}")
        return make_response("success", data={"path": str(full_path.relative_to(WORK_DIR)), "deleted": True})

# ---------- WebSocket 处理 ----------
async def websocket_handler(websocket):
    try:
        async for message in websocket:
        # ---------- 打印原始消息（调试） ----------
            logger.info(f"收到消息类型: {type(message)}")
            if isinstance(message, bytes):
                # 二进制帧，尝试用 UTF-8 解码
                try:
                    decoded = message.decode('utf-8')
                    logger.info(f"原始字节 (repr): {repr(message)}")
                    logger.info(f"解码后字符串: {decoded}")
                except UnicodeDecodeError:
                    logger.error(f"无法用 UTF-8 解码收到的二进制数据: {repr(message)}")
            else:
                # 文本帧，直接是 str
                logger.info(f"收到文本消息: {repr(message)}")
            try:
                data = json.loads(message)
                request_id = data.get('id')

                # ---------- 新增：处理 AI 响应（无 action） ----------
                if 'action' not in data:
                    # 视为 AI 响应，尝试解析并执行
                    result = process_ai_response(data, request_id)
                    if result is None:
                        continue
                    if request_id:
                        result['id'] = request_id
                    await websocket.send(json.dumps(result))
                    continue

                # ---------- 原有 action 处理 ----------
                action = data.get('action')
                if action == 'prompt':
                    # prompt 逻辑不变
                    try:
                        prompt = load_prompt()
                        result = make_response("success", data={"prompt": prompt})
                    except Exception as e:
                        logger.error(f"获取提示词失败: {e}")
                        result = make_response("error", message=str(e))
                    if request_id:
                        result['id'] = request_id
                    await websocket.send(json.dumps(result))
                    continue

                # 其他 action 调用统一的执行函数
                result = execute_action(data, request_id)
                await websocket.send(json.dumps(result))

            except json.JSONDecodeError:
                await websocket.send(json.dumps(make_response("error", message="无效的 JSON")))
    except websockets.exceptions.ConnectionClosed:
        logger.info("WebSocket 连接关闭")
    except Exception as e:
        logger.error(f"WebSocket 处理异常: {e}")
def execute_action(data, request_id=None):
    """
    根据 data 中的 action 执行文件操作，返回响应字典（包含 id 如果需要）
    """
    action = data.get('action')
    if action not in ['create', 'read', 'delete', 'list', 'overwrite', 'append', 'replace']:
        return make_response("error", message="不支持的 action")

    if action == 'list':
        try:
            result = handle_list(data)
        except Exception as e:
            logger.error(f"列表操作失败: {e}")
            result = make_response("error", message=str(e))
        if request_id:
            result['id'] = request_id
        return result

    file_path = data.get('path')
    if not file_path:
        return make_response("error", message="缺少 path 参数")
    try:
        full_path = safe_path(file_path)
    except ValueError as e:
        return make_response("error", message=str(e))

    try:
        if action == 'create':
            content = data.get('content', '')
            result = handle_create(full_path, content)
        elif action == 'read':
            offset = int(data.get('offset', 0))
            limit = data.get('limit')
            if limit is not None:
                limit = int(limit)
            tail = data.get('tail', False)
            if tail and limit is None:
                limit = 10
            result = handle_read(full_path, offset, limit, tail)
        elif action == 'overwrite':
            content = data.get('content', '')
            result = handle_overwrite(full_path, content)
        elif action == 'append':
            content = data.get('content', '')
            result = handle_append(full_path, content)
        elif action == 'replace':
            search = data.get('search')
            replace = data.get('replace')
            if search is None or replace is None:
                return make_response("error", message="replace 操作需要提供 search 和 replace 参数")
            count = data.get('count', 1)
            if count is not None:
                count = int(count)
            result = handle_replace(full_path, search, replace, count)
        elif action == 'delete':
            recursive = data.get('recursive', False)
            result = handle_delete(full_path, recursive)
        else:
            result = make_response("error", message="未知操作")
        if request_id:
            result['id'] = request_id
        return result
    except FileNotFoundError as e:
        return make_response("error", message=str(e))
    except IsADirectoryError as e:
        return make_response("error", message=str(e))
    except PermissionError as e:
        return make_response("error", message=str(e))
    except Exception as e:
        logger.error(f"操作失败: {e}")
        return make_response("error", message="内部服务器错误")
# ---------- 启动 WebSocket 服务 ----------
if __name__ == '__main__':
    if websockets is None:
        print("❌ 未安装 websockets 库，请执行: pip install websockets")
        exit(1)

    print(f"🚀 Agent WebSocket 服务启动")
    print(f"📁 工作目录: {WORK_DIR}")
    print(f"🔌 WebSocket 监听地址: {WS_HOST}:{WS_PORT}")
    print(f"📋 日志文件: {LOG_FILE} (轮转: 10MB/5备份)")
    prompt_file = AGENT_DIR / "prompt.md"
    print(f"📝 提示词来源: {prompt_file if prompt_file.exists() else '内置默认'}")

    async def start_ws():
        async with websockets.serve(websocket_handler, WS_HOST, WS_PORT):
            await asyncio.Future()  # 永久运行

    try:
        asyncio.run(start_ws())
    except KeyboardInterrupt:
        print("服务已停止")