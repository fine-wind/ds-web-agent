# sandbox.py
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import platform
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


# =========================================================
# 沙箱配置
# =========================================================
class SandboxConfig:
    def __init__(
            self,
            root: str = r"D:\temp",
            read_only: bool = False,
            allow_shell: bool = True,
            allow_python: bool = True,
            allow_network: bool = True,
            max_file_bytes: int = 5 * 1024 * 1024,  # 单文件最大 5MB
            max_output_chars: int = 20000,  # 输出截断
            default_timeout: int = 30,
            max_timeout: int = 120,
            shell_whitelist=None,
            allowed_hosts=None,
            audit_log: str = None,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

        self.read_only = read_only
        self.allow_shell = allow_shell
        self.allow_python = allow_python
        self.allow_network = allow_network
        self.max_file_bytes = max_file_bytes
        self.max_output_chars = max_output_chars
        self.default_timeout = default_timeout
        self.max_timeout = max_timeout

        # 默认 shell 白名单：只允许这些命令前缀
        self.shell_whitelist = shell_whitelist or [
            "dir", "ls", "type", "cat", "echo", "find", "findstr",
            "grep", "where", "which", "python", "python3", "pip",
            "git", "node", "npm",
        ]

        # 网络白名单：None 表示全部放行
        self.allowed_hosts = allowed_hosts

        self.audit_log = Path(audit_log) if audit_log else (self.root / "audit.log")
        self._lock = threading.Lock()


CFG = SandboxConfig()


# =========================================================
# 异常 & 审计
# =========================================================
class SandboxError(Exception):
    pass


def _audit(tool: str, args: dict, ok: bool, extra: str = ""):
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tool": tool,
        "args": args,
        "ok": ok,
        "extra": extra,
    }
    try:
        with CFG._lock:
            log_path = CFG.audit_log
            try:
                if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
                    backup = log_path.with_suffix(log_path.suffix + ".1")
                    if backup.exists():
                        try:
                            backup.unlink()
                        except OSError:
                            pass
                    try:
                        log_path.rename(backup)
                    except OSError:
                        pass
            except OSError:
                pass
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# =========================================================
# 路径沙箱
# =========================================================
def safe_path(user_path: str, must_exist: bool = False) -> Path:
    """
    把用户输入的路径解析到沙箱内部。任何逃逸都会抛 SandboxError。
    """
    if user_path is None:
        user_path = "."

    p = Path(user_path)

    # 相对路径 -> 相对沙箱根
    if not p.is_absolute():
        candidate = (CFG.root / p)
    else:
        candidate = p

    # 解析 ..、符号链接
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as e:
        raise SandboxError(f"路径解析失败: {e}") from e

    # 必须落在沙箱根内
    try:
        resolved.relative_to(CFG.root)
    except ValueError:
        raise SandboxError(f"路径越界，禁止访问沙箱外: {user_path}")

    if must_exist and not resolved.exists():
        raise SandboxError(f"路径不存在: {user_path}")

    return resolved


def _check_write_allowed():
    if CFG.read_only:
        raise SandboxError("当前沙箱为只读模式，禁止写操作")


def _clip(text: str) -> str:
    limit = CFG.max_output_chars
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，共 {len(text)} 字符]"


# =========================================================
# 文件工具
# =========================================================
def read_file(path: str, encoding: str = "utf-8"):
    p = safe_path(path, must_exist=True)
    if not p.is_file():
        raise SandboxError(f"不是文件: {path}")

    size = p.stat().st_size
    if size > CFG.max_file_bytes:
        raise SandboxError(f"文件过大 ({size} bytes > {CFG.max_file_bytes})")

    content = p.read_text(encoding=encoding)
    _audit("read_file", {"path": path}, True)
    return f"文件：{p.relative_to(CFG.root)}\n----------\n大小：{size}\n----------\n{content}"


def write_file(path: str, content: str, encoding: str = "utf-8", append: bool = False):
    _check_write_allowed()
    p = safe_path(path)

    data = content.encode(encoding)
    if len(data) > CFG.max_file_bytes:
        raise SandboxError(f"写入内容过大 ({len(data)} bytes)")

    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with p.open(mode, encoding=encoding) as f:
        f.write(content)

    _audit("write_file", {"path": path, "append": append, "bytes": len(data)}, True)
    return {
        "path": str(p.relative_to(CFG.root)),
        "bytes": len(data),
        "append": append,
    }


def list_directory(path: str = ".", recursive: bool = False):
    p = safe_path(path, must_exist=True)
    if not p.is_dir():
        raise SandboxError(f"不是目录: {path}")

    items = []
    iterator = p.rglob("*") if recursive else p.iterdir()

    for child in iterator:
        try:
            stat = child.stat()
            items.append(
                f"名称：{str(child.relative_to(p)) if recursive else child.name}，是否文件夹：{child.is_dir()}，大小：{stat.st_size if child.is_file() else None}")
        except OSError:
            continue
        if len(items) >= 500:
            break

    _audit("list_directory", {"path": path, "recursive": recursive}, True)
    tail = "\n".join(items)
    return f"路径：{str(p.relative_to(CFG.root))}，数量：{len(items)}\n" + tail


def delete_file(path: str):
    _check_write_allowed()
    p = safe_path(path, must_exist=True)
    if p.is_dir():
        raise SandboxError("delete_file 只支持文件，目录请用 delete_directory")

    p.unlink()
    _audit("delete_file", {"path": path}, True)
    return {"deleted": str(p.relative_to(CFG.root))}


def delete_directory(path: str, recursive: bool = False):
    _check_write_allowed()
    p = safe_path(path, must_exist=True)
    if not p.is_dir():
        raise SandboxError(f"不是目录: {path}")
    if p == CFG.root:
        raise SandboxError("禁止删除沙箱根目录")

    if recursive:
        for child in sorted(p.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                try:
                    child.rmdir()
                except OSError:
                    pass
        p.rmdir()
    else:
        p.rmdir()

    _audit("delete_directory", {"path": path, "recursive": recursive}, True)
    return {"deleted": str(p.relative_to(CFG.root)), "recursive": recursive}


def search_files(pattern: str = "*", path: str = ".", max_results: int = 50):
    root = safe_path(path, must_exist=True)
    results = []
    for p in root.rglob(pattern):
        # 每个结果也要验证一遍，防止符号链接逃逸
        try:
            safe_path(str(p))
        except SandboxError:
            continue
        results.append(str(p.relative_to(CFG.root)))
        if len(results) >= max_results:
            break

    _audit("search_files", {"pattern": pattern, "path": path}, True)
    return {"pattern": pattern, "count": len(results), "files": results}


def _compile_safe_regex(pattern: str):
    if len(pattern) > 200:
        raise SandboxError("正则表达式过长（>200 字符）")
    if re.search(r"(\([^)]*[+*][^)]*\))[+*{]", pattern):
        raise SandboxError("疑似灾难性回溯的正则")
    try:
        return re.compile(pattern)
    except re.error as e:
        raise SandboxError(f"正则表达式无效: {e}")


def grep(pattern: str, path: str = ".", file_glob: str = "*", max_results: int = 50):
    root = safe_path(path, must_exist=True)
    regex = _compile_safe_regex(pattern)
    matches = []

    for p in root.rglob("*"):
        if not p.is_file() or not fnmatch.fnmatch(p.name, file_glob):
            continue
        try:
            safe_path(str(p))
        except SandboxError:
            continue

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                matches.append({
                    "file": str(p.relative_to(CFG.root)),
                    "line": lineno,
                    "text": _clip(line),
                })
                if len(matches) >= max_results:
                    _audit("grep", {"pattern": pattern}, True)
                    return {"pattern": pattern, "count": len(matches), "matches": matches}

    _audit("grep", {"pattern": pattern}, True)
    return {"pattern": pattern, "count": len(matches), "matches": matches}


# =========================================================
# Shell 沙箱
# =========================================================
_SHELL_BLOCK_PATTERNS = [
    r"\.\.\\", r"\.\./",  # 目录穿越
    r"[&|;`$]\s*\(",  # 子 shell / 命令替换
    r"\brm\s+-rf\s+/",  # 危险 rm
    r"\bdel\s+/s\s+/q\s+[a-zA-Z]:",  # 危险 del
    r"\bformat\b",
    r"\bshutdown\b", r"\breboot\b",
    r"\bcurl\b.*\|\s*\b(sh|bash|powershell)\b",  # 管道下载执行
    r"\bwget\b.*\|\s*\b(sh|bash|powershell)\b",
    r"\bpowershell\b.*\-(enc|encodedcommand)\b",
    r"\bcmd\b\s*/(c|k)\b.*[&|;]",
]


def _has_shell_meta(command: str) -> bool:
    """检查命令在引号外是否含 shell 元字符（连接符/重定向/替换）。"""
    stripped = re.sub(r"'[^']*'", "", command)
    stripped = re.sub(r'"[^"]*"', "", stripped)
    return bool(re.search(r"[&|;\x60$<>]", stripped))


def _sanitized_env():
    """只保留最基础的环境变量，剥离敏感信息。"""
    keep = {"PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP",
            "PATHEXT", "COMSPEC", "HOME", "USERPROFILE", "LANG", "LC_ALL"}
    env = {k: v for k, v in os.environ.items() if k in keep}

    # 兜底：剔除任何包含敏感关键字的变量
    for k in list(env.keys()):
        if re.search(r"(TOKEN|KEY|SECRET|PASS|CREDENTIAL)", k, re.I):
            env.pop(k, None)

    env["SANDBOX_ROOT"] = str(CFG.root)
    return env


def _check_shell_command(command: str):
    if not CFG.allow_shell:
        raise SandboxError("沙箱未开启 shell 执行")

    for pat in _SHELL_BLOCK_PATTERNS:
        if re.search(pat, command, re.I):
            raise SandboxError(f"命令命中黑名单规则: {pat}")

    # 解析首个 token，检查白名单
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()

    if not parts:
        raise SandboxError("空命令")

    head = Path(parts[0]).name.lower()
    if head.endswith(".exe"):
        head = head[:-4]

    if _has_shell_meta(command):
        raise SandboxError("命令包含 shell 元字符，拒绝执行")

    if CFG.shell_whitelist:
        allowed = [w.lower() for w in CFG.shell_whitelist]
        if head not in allowed:
            raise SandboxError(
                f"命令 '{head}' 不在白名单内。允许: {CFG.shell_whitelist}"
            )


def execute_shell(command: str, cwd: str = None, timeout: int = None):
    _check_shell_command(command)

    workdir = safe_path(cwd or ".")
    if not workdir.is_dir():
        raise SandboxError(f"工作目录无效: {cwd}")

    timeout = min(timeout or CFG.default_timeout, CFG.max_timeout)

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_sanitized_env(),
        )
        ok = proc.returncode == 0
        _audit("execute_shell", {"command": command, "cwd": cwd}, ok,
               f"exit={proc.returncode}")
        return f"exit_code:{proc.returncode}, stdout:{_clip(proc.stdout)}, stderr:{_clip(proc.stderr)}\n"
    except subprocess.TimeoutExpired:
        _audit("execute_shell", {"command": command}, False, "timeout")
        raise SandboxError(f"命令超时 (>{timeout}s)")


def python_exec(code: str, timeout: int = None):
    if not CFG.allow_python:
        raise SandboxError("沙箱未开启 python 执行")

    timeout = min(timeout or CFG.default_timeout, CFG.max_timeout)

    # 前置一段注入代码，把工作目录锁定到沙箱根
    root_literal = repr(str(CFG.root))
    preamble = (
        "import os\n"
        f"os.chdir({root_literal})\n"
        f"os.environ['SANDBOX_ROOT'] = {root_literal}\n"
    )
    full_code = preamble + code

    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", full_code],  # -I 隔离模式
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(CFG.root),
            env=_sanitized_env(),
        )
        ok = proc.returncode == 0
        _audit("python_exec", {"code_len": len(code)}, ok,
               f"exit={proc.returncode}")
        return f"exit_code:{proc.returncode}, stdout:{_clip(proc.stdout)}, stderr:{_clip(proc.stderr)}\n"

    except subprocess.TimeoutExpired:
        _audit("python_exec", {"code_len": len(code)}, False, "timeout")
        raise SandboxError(f"Python 执行超时 (>{timeout}s)")


# =========================================================
# 网络沙箱
# =========================================================
def _check_host(url: str):
    if not CFG.allow_network:
        raise SandboxError("沙箱未开启网络访问")

    from urllib.parse import urlparse
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SandboxError(f"只允许 http/https，当前 scheme: {scheme or '空'}")

    if CFG.allowed_hosts is None:
        return

    host = parsed.hostname or ""
    if not any(host == h or host.endswith("." + h) for h in CFG.allowed_hosts):
        raise SandboxError(f"目标主机 {host} 不在白名单内")


def http_request(
        url: str,
        method: str = "GET",
        headers: dict = None,
        params: dict = None,
        json_body: dict = None,
        data: str = None,
        timeout: int = 30,
):
    _check_host(url)

    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode(params)}"

    h = dict(headers or {})
    body = None

    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    elif data is not None:
        body = str(data).encode("utf-8")

    req = Request(url, data=body, headers=h, method=method.upper())

    try:
        with urlopen(req, timeout=min(timeout, CFG.max_timeout)) as resp:
            raw = resp.read(CFG.max_file_bytes)
            text = raw.decode("utf-8", errors="replace")
            parsed = None
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                pass

            _audit("http_request", {"url": url, "method": method}, True,
                   f"status={resp.status}")
            return {
                "status": resp.status,
                "headers": dict(resp.headers),
                "text": _clip(text),
                "json": parsed,
            }
    except Exception as e:
        _audit("http_request", {"url": url}, False, str(e))
        raise SandboxError(f"HTTP 请求失败: {type(e).__name__}: {e}")


# =========================================================
# 无副作用工具
# =========================================================
def _safe_pow(a, b):
    if isinstance(b, (int, float)) and abs(b) > 1000:
        raise SandboxError("指数过大")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if abs(a) > 1 and b > 100:
            raise SandboxError("幂运算结果可能过大")
    return a ** b


_BIN_OPS = {
    "Add": lambda a, b: a + b,
    "Sub": lambda a, b: a - b,
    "Mult": lambda a, b: a * b,
    "Div": lambda a, b: a / b,
    "FloorDiv": lambda a, b: a // b,
    "Mod": lambda a, b: a % b,
    "Pow": _safe_pow,
}
_UNARY_OPS = {"UAdd": lambda a: +a, "USub": lambda a: -a}


def git_info():
    """
    返回沙箱运行环境的基本系统信息，以及沙箱根目录的 Git 状态（如果适用）。
    """
    info = {
        "os_system": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "python_version": sys.version.split()[0],
        "sandbox_root": str(CFG.root),
        "read_only_mode": CFG.read_only,
    }

    # 尝试安全地获取沙箱根目录的 Git 信息
    if CFG.allow_shell:
        try:
            # 1. 检查是否是 git 仓库
            rev_parse = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(CFG.root),
                capture_output=True,
                text=True,
                timeout=3
            )
            if rev_parse.returncode == 0 and rev_parse.stdout.strip() == "true":
                info["is_git_repo"] = True

                # 2. 获取当前分支
                branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=str(CFG.root),
                    capture_output=True,
                    text=True,
                    timeout=3
                )
                if branch.returncode == 0:
                    info["git_branch"] = branch.stdout.strip() or "detached HEAD"

                # 3. 获取最新 commit 短哈希
                commit = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=str(CFG.root),
                    capture_output=True,
                    text=True,
                    timeout=3
                )
                if commit.returncode == 0:
                    info["git_commit"] = commit.stdout.strip()
            else:
                info["is_git_repo"] = False
        except Exception:
            info["git_status"] = "check_failed_or_git_not_installed"
    else:
        info["git_status"] = "shell_execution_disabled_in_sandbox"

    _audit("git_info", {}, True)
    return info


def _eval_node(node):
    import ast
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise SandboxError("只支持数字常量")
    if isinstance(node, ast.BinOp):
        fn = _BIN_OPS.get(type(node.op).__name__)
        if not fn:
            raise SandboxError("不支持的运算符")
        return fn(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        fn = _UNARY_OPS.get(type(node.op).__name__)
        if not fn:
            raise SandboxError("不支持的一元运算符")
        return fn(_eval_node(node.operand))
    raise SandboxError("表达式包含不支持的内容")


def calculate(expression: str):
    import ast
    tree = ast.parse(expression, mode="eval")
    result = _eval_node(tree)
    _audit("calculate", {"expression": expression}, True)
    return {"expression": expression, "result": result}


def get_current_time(timezone: str = "Asia/Shanghai"):
    if ZoneInfo is not None:
        now = datetime.now(ZoneInfo(timezone))
    else:
        now = datetime.now()
    return {
        "timezone": timezone,
        "datetime": now.isoformat(),
        "timestamp": now.timestamp(),
    }


# =========================================================
# 记忆
# =========================================================
def _memory_file() -> Path:
    return Path("agent_memory.json")


def memory_save(key: str, value: str):
    _check_write_allowed()
    p = _memory_file()
    mem = {}
    if p.exists():
        try:
            mem = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            mem = {}
    mem[key] = value
    p.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
    _audit("memory_save", {"key": key}, True)
    return {"ok": True, "key": key}


def memory_recall(key: str):
    p = _memory_file()
    if not p.exists():
        return {"key": key, "value": None}
    try:
        mem = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        mem = {}
    return {"key": key, "value": mem.get(key)}


# =========================================================
# 工具注册（schema + 分发）
# =========================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取沙箱内的文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对沙箱根的路径"},
                    "encoding": {"type": "string",
                                 "enum": ["utf-8", "gbk", "ascii"],
                                 "default": "utf-8"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入沙箱内文件，可追加。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "encoding": {"type": "string",
                                 "enum": ["utf-8", "gbk", "ascii"],
                                 "default": "utf-8"},
                    "append": {"type": "boolean", "default": False},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出沙箱内目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "recursive": {"type": "boolean", "default": False},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除沙箱内文件。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_directory",
            "description": "删除沙箱内目录（可递归）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "recursive": {"type": "boolean", "default": False},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "在沙箱内按 glob 搜索文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "default": "*"},
                    "path": {"type": "string", "default": "."},
                    "max_results": {"type": "integer", "default": 50},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "在沙箱内按正则搜索文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "file_glob": {"type": "string", "default": "*"},
                    "max_results": {"type": "integer", "default": 50},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_shell",
            "description": "在沙箱工作目录下执行白名单内的 shell 命令。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "python_exec",
            "description": "在沙箱隔离环境中执行 Python 代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "timeout": {"type": "integer", "default": 10},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": "发起 HTTP 请求（受主机白名单限制）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string", "default": "GET"},
                    "headers": {"type": "object"},
                    "params": {"type": "object"},
                    "json_body": {"type": "object"},
                    "data": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "计算数学表达式。",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前时间。",
            "parameters": {
                "type": "object",
                "properties": {"timezone": {"type": "string",
                                            "default": "Asia/Shanghai"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "保存记忆（写入沙箱内 agent_memory.json）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_recall",
            "description": "读取记忆。",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_info",
            "description": "获取沙箱运行环境的基本系统信息（OS、Python版本等），以及沙箱根目录的 Git 仓库状态（分支、Commit等）。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "read_file": read_file,
    "write_file": write_file,
    "list_directory": list_directory,
    "delete_file": delete_file,
    "delete_directory": delete_directory,
    "search_files": search_files,
    "grep": grep,
    "execute_shell": execute_shell,
    "python_exec": python_exec,
    "http_request": http_request,
    "calculate": calculate,
    "get_current_time": get_current_time,
    "memory_save": memory_save,
    "memory_recall": memory_recall,
    "git_info": git_info,
}


def execute_tool(name: str, arguments):
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as e:
            return f"参数不是合法 JSON: {e}"

    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return f"未知工具: {name}"

    try:
        result = fn(**(arguments or {}))
    except SandboxError as e:
        result = {"error": f"沙箱拒绝: {e}"}
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}

    # 统一出口：字符串直接返回，其他转 JSON 文本
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)
