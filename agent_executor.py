#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import logging
import subprocess
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
import shutil

from flask import Flask, request, jsonify
from flask_cors import CORS

# ---------- 配置（支持环境变量） ----------
APP_HOST = os.getenv("AGENT_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("AGENT_PORT", 8888))
WORK_DIR_ENV = os.getenv("AGENT_WORK_DIR", None)
CORS_ORIGINS = os.getenv("AGENT_CORS_ORIGINS", "*")   # 生产环境请设置具体域名，如 "https://example.com"

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
# 同时输出到控制台（便于调试）
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

    # 读取所有行（适用于文本文件）
    with open(full_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    total_lines = len(lines)

    if tail:
        # 如果 tail=True，取末尾 limit 行，默认 limit=10
        if limit is None:
            limit = 10
        start = max(0, total_lines - limit)
        selected = lines[start:]
    else:
        # 正常分页
        start = max(0, offset)
        if limit is not None and limit > 0:
            end = min(start + limit, total_lines)
            selected = lines[start:end]
        else:
            selected = lines[start:]

    selected_text = ''.join(selected)
    # 返回时附上总行数和实际返回行数
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
    """覆盖写入文件"""
    if not full_path.exists():
        raise FileNotFoundError(f'文件不存在: {full_path.relative_to(WORK_DIR)}')
    if full_path.is_dir():
        raise IsADirectoryError(f'路径是目录，不能执行此操作: {full_path.relative_to(WORK_DIR)}')
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(content or '')
    logger.info(f"覆盖文件: {full_path.relative_to(WORK_DIR)}")
    return make_response("success", data={"path": str(full_path.relative_to(WORK_DIR)), "size": full_path.stat().st_size})

def handle_append(full_path, content):
    """追加内容到文件末尾"""
    if not full_path.exists():
        raise FileNotFoundError(f'文件不存在: {full_path.relative_to(WORK_DIR)}')
    if full_path.is_dir():
        raise IsADirectoryError(f'路径是目录，不能执行此操作: {full_path.relative_to(WORK_DIR)}')
    with open(full_path, 'a', encoding='utf-8') as f:
        f.write(content or '')
    logger.info(f"追加文件: {full_path.relative_to(WORK_DIR)}")
    return make_response("success", data={"path": str(full_path.relative_to(WORK_DIR)), "size": full_path.stat().st_size})

def handle_replace(full_path, search, replace, count=1):
    """搜索替换（方案三）"""
    if not full_path.exists():
        raise FileNotFoundError(f'文件不存在: {full_path.relative_to(WORK_DIR)}')
    if full_path.is_dir():
        raise IsADirectoryError(f'路径是目录，不能执行此操作: {full_path.relative_to(WORK_DIR)}')
    with open(full_path, 'r', encoding='utf-8') as f:
        old_text = f.read()
    import re
    # count=0 表示不替换，count=-1 表示替换全部，count>0 替换指定次数
    if count == 0:
        new_text = old_text
        replaced_count = 0
    else:
        subn_count = 0 if count == -1 else count
        new_text, replaced_count = re.subn(re.escape(search), replace, old_text, count=subn_count)
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(new_text)
    logger.info(f"替换文件: {full_path.relative_to(WORK_DIR)}，替换次数: {replaced_count}")
    return make_response("success", data={
        "path": str(full_path.relative_to(WORK_DIR)),
        "size": full_path.stat().st_size,
        "replaced_count": replaced_count,
        "search": search,
        "replace": replace,
        "count": count
    })

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

# ---------- Flask 应用 ----------
app = Flask(__name__)
# CORS 配置
if CORS_ORIGINS == "*":
    CORS(app)
else:
    origins = [origin.strip() for origin in CORS_ORIGINS.split(',') if origin.strip()]
    CORS(app, origins=origins)

@app.route('/health', methods=['GET'])
def health():
    return jsonify(make_response("success", data={"status": "running", "workspace": str(WORK_DIR)}))

@app.route('/prompt', methods=['GET'])
def get_prompt():
    return jsonify(make_response("success", data={"prompt": load_prompt()}))

@app.route('/file', methods=['POST'])
def file_operations():
    try:
        data = request.get_json()
        if data is None:
            return jsonify(make_response("error", message="无效的JSON")), 400

        action = data.get('action')
        if action not in ['create', 'read', 'delete', 'list', 'overwrite', 'append', 'replace']:
            return jsonify(make_response("error", message="不支持的 action，可选: create/read/delete/list/overwrite/append/replace")), 400

        if action == 'list':
            try:
                result = handle_list(data)
                return jsonify(result), 200
            except Exception as e:
                logger.error(f"列表操作失败: {e}")
                return jsonify(make_response("error", message=str(e))), 500

        file_path = data.get('path')
        if not file_path:
            return jsonify(make_response("error", message="缺少 path 参数")), 400

        try:
            full_path = safe_path(file_path)
        except ValueError as e:
            return jsonify(make_response("error", message=str(e))), 400

        try:
            if action == 'create':
                content = data.get('content', '')
                result = handle_create(full_path, content)
                return jsonify(result), 201
            elif action == 'read':
                offset = int(data.get('offset', 0))
                limit = data.get('limit')   # 可能为 None
                if limit is not None:
                    limit = int(limit)
                tail = data.get('tail', False)
                # 若 tail=True 且 limit 未提供，默认设置为 10
                if tail and limit is None:
                    limit = 10
                result = handle_read(full_path, offset, limit, tail)
                return jsonify(result), 200
            elif action == 'overwrite':
                content = data.get('content', '')
                result = handle_overwrite(full_path, content)
                return jsonify(result), 200

            elif action == 'append':
                content = data.get('content', '')
                result = handle_append(full_path, content)
                return jsonify(result), 200

            elif action == 'replace':
                search = data.get('search')
                replace = data.get('replace')
                if search is None or replace is None:
                    return jsonify(make_response("error", message="replace 操作需要提供 search 和 replace 参数")), 400
                count = data.get('count', 1)
                if count is not None:
                    count = int(count)
                result = handle_replace(full_path, search, replace, count)
                return jsonify(result), 200
            elif action == 'delete':
                recursive = data.get('recursive', False)
                result = handle_delete(full_path, recursive)
                return jsonify(result), 200
        except FileNotFoundError as e:
            return jsonify(make_response("error", message=str(e))), 404
        except IsADirectoryError as e:
            return jsonify(make_response("error", message=str(e))), 400
        except PermissionError as e:
            return jsonify(make_response("error", message=str(e))), 403
        except Exception as e:
            logger.error(f"操作失败: {e}")
            return jsonify(make_response("error", message="内部服务器错误")), 500

    except Exception as e:
        logger.error(f"请求处理异常: {e}")
        return jsonify(make_response("error", message="服务器内部错误")), 500

if __name__ == '__main__':
    print(f"🚀 Agent 执行服务启动")
    print(f"📁 工作目录: {WORK_DIR}")
    print(f"🌐 监听地址: {APP_HOST}:{APP_PORT}")
    print(f"📋 日志文件: {LOG_FILE} (轮转: 10MB/5备份)")
    print(f"📝 提示词来源: {AGENT_DIR / 'prompt.md' if (AGENT_DIR / 'prompt.md').exists() else '内置默认'}")
    print(f"🔒 CORS 允许来源: {CORS_ORIGINS}")
    print("按 Ctrl+C 停止服务")
    app.run(host=APP_HOST, port=APP_PORT, debug=False)