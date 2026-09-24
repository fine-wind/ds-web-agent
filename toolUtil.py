# sandbox.py
import fnmatch
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


# =========================================================
# 配置（已去掉沙箱约束）
# =========================================================
class SandboxConfig:
    def __init__(
            self,
            root: str = "/work",
            agent_zone: str = "/app",
            read_only: bool = False,
            allow_shell: bool = True,
            allow_python: bool = True,
            allow_network: bool = True,
            max_file_bytes: int = 5 * 1024 * 1024,   # 单文件最大 5MB
            max_output_chars: int = 20000,            # 输出截断
            default_timeout: int = 30,
            max_timeout: int = 120,
            shell_whitelist=None,
            allowed_hosts=None,
            audit_log: str = None,
    ):
        self.root = Path(root).resolve()
        self.agent_zone = Path(agent_zone).resolve()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        self.read_only = read_only
        self.allow_shell = allow_shell
        self.allow_python = allow_python
        self.allow_network = allow_network
        self.max_file_bytes = max_file_bytes
        self.max_output_chars = max_output_chars
        self.default_timeout = default_timeout
        self.max_timeout = max_timeout

        # 空列表 = 不做白名单检查
        self.shell_whitelist = shell_whitelist or []
        # None = 全部放行
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
    if ZoneInfo is not None:
        try:
            _ts = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        except Exception:
            _ts = datetime.now().isoformat(timespec="seconds")
    else:
        _ts = datetime.now().isoformat(timespec="seconds")
    entry = {
        "ts": _ts,
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
# 路径解析（不再限制在 root 内）
# =========================================================
def safe_path(user_path: str, must_exist: bool = False) -> Path:
    if user_path is None:
        user_path = "."

    p = Path(user_path)

    # 相对路径 -> 相对 CFG.root；绝对路径 -> 原样使用
    if not p.is_absolute():
        candidate = (CFG.root / p)
    else:
        candidate = p

    try:
        resolved = candidate.resolve(strict=False)
    except OSError as e:
        raise SandboxError(f"路径解析失败: {e}") from e

    _allowed_roots = [CFG.root]
    _agent_zone = getattr(CFG, "agent_zone", None)
    if _agent_zone is not None:
        _allowed_roots.append(_agent_zone)
    _ok = False
    for _r in _allowed_roots:
        try:
            resolved.relative_to(_r)
            _ok = True
            break
        except ValueError:
            continue
    if not _ok:
        raise SandboxError(f"路径越界，不允许访问沙箱根之外: {user_path}")

    if must_exist and not resolved.exists():
        raise SandboxError(f"路径不存在: {user_path}")

    return resolved


def _rel(p: Path) -> str:
    """尽量返回相对 CFG.root 的路径；在 root 之外时返回绝对路径。"""
    try:
        return str(p.relative_to(CFG.root))
    except ValueError:
        return str(p)


def _clip(text: str) -> str:
    limit = CFG.max_output_chars
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，共 {len(text)} 字符]"


# =========================================================
# 只读模式守卫
# =========================================================
def _check_writable():
    if CFG.read_only:
        raise SandboxError("沙箱处于只读模式，禁止写入操作")


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
    return f"文件：{_rel(p)}\n----------\n大小：{size}\n----------\n{content}"


def write_file(path: str, content: str, encoding: str = "utf-8", append: bool = False):
    _check_writable()
    p = safe_path(path)

    data = content.encode(encoding)
    if len(data) > CFG.max_file_bytes:
        raise SandboxError(f"写入内容过大 ({len(data)} bytes)")

    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with p.open(mode, encoding=encoding, newline="\n") as f:
        f.write(content)

    _audit("write_file", {"path": path, "append": append, "bytes": len(data)}, True)
    return {
        "path": _rel(p),
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
                f"名称：{child.relative_to(p).as_posix() if recursive else child.name}，"
                f"是否文件夹：{child.is_dir()}，"
                f"大小：{stat.st_size if child.is_file() else None}"
            )
        except OSError:
            continue
        if len(items) >= 500:
            break

    _audit("list_directory", {"path": path, "recursive": recursive}, True)
    tail = "\n".join(items)
    return f"路径：{_rel(p)}，数量：{len(items)}\n" + tail


def delete_file(path: str):
    _check_writable()
    p = safe_path(path, must_exist=True)
    if p.is_dir():
        raise SandboxError("delete_file 只支持文件，目录请用 delete_directory")

    p.unlink()
    _audit("delete_file", {"path": path}, True)
    return {"deleted": _rel(p)}


def delete_directory(path: str, recursive: bool = False):
    _check_writable()
    p = safe_path(path, must_exist=True)
    if not p.is_dir():
        raise SandboxError(f"不是目录: {path}")

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
    return {"deleted": _rel(p), "recursive": recursive}


def search_files(pattern: str = "*", path: str = ".", max_results: int = 50):
    root = safe_path(path, must_exist=True)
    results = []
    for p in root.rglob(pattern):
        results.append(_rel(p))
        if len(results) >= max_results:
            break

    _audit("search_files", {"pattern": pattern, "path": path}, True)
    return {"pattern": pattern, "count": len(results), "files": results}


def _compile_safe_regex(pattern: str):
    if len(pattern) > 2000:
        raise SandboxError("正则表达式过长（>2000 字符）")
    try:
        return re.compile(pattern)
    except re.error as e:
        raise SandboxError(f"正则表达式无效: {e}")


def grep(pattern: str, path: str = ".", file_glob: str = "*", max_results: int = 50):
    root = safe_path(path, must_exist=True)
    regex = _compile_safe_regex(pattern)
    matches = []

    if root.is_file():
        candidates = [root]
    else:
        candidates = [p for p in root.rglob("*") if p.is_file()]

    for p in candidates:
        if not fnmatch.fnmatch(p.name, file_glob):
            continue

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                matches.append({
                    "file": _rel(p),
                    "line": lineno,
                    "text": _clip(line),
                })
                if len(matches) >= max_results:
                    _audit("grep", {"pattern": pattern}, True)
                    return {"pattern": pattern, "count": len(matches), "matches": matches}

    _audit("grep", {"pattern": pattern}, True)
    return {"pattern": pattern, "count": len(matches), "matches": matches}


# =========================================================
# Shell 安全策略（Linux）
# =========================================================
_SHELL_DANGEROUS_PATTERNS = [
    (r'\brm\s+(-[a-zA-Z]+\s+)*/(\s|$)', '禁止删除根目录'),
    (r'\bdd\s+.*of=/dev/(sd|hd|nvme|vd|mmcblk)', '禁止写入块设备'),
    (r'\bmkfs(\.\w+)?\b', '禁止格式化文件系统'),
    (r'\b(shutdown|reboot|halt|poweroff)\b', '禁止关机/重启'),
    (r'\binit\s+[06]\b', '禁止 init 0/6'),
    (r':\(\)\s*\{.*?\}\s*;\s*:', '禁止 fork bomb'),
    (r'>\s*/etc/(passwd|shadow|sudoers|fstab|hosts)', '禁止写入系统关键文件'),
    (r'\b(curl|wget)\s+[^|]*\|\s*(sh|bash|zsh)\b', '禁止远程脚本直接执行'),
    (r'\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/(\s|$)', '禁止对根目录 chmod 777'),
    (r'\bexport\s+(PATH|LD_PRELOAD|LD_LIBRARY_PATH)\s*=', '禁止覆盖关键环境变量'),
    (r'>\s*/dev/(sd|hd|nvme|vd|mmcblk)', '禁止写入块设备'),
]

_SENSITIVE_ENV_KEYS = (
    'KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'PASSWD',
    'CREDENTIAL', 'API_KEY', 'PRIVATE',
)


def _check_shell_safety(command: str):
    for pattern, reason in _SHELL_DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            raise SandboxError(f"命令被安全策略拒绝: {reason}")


def _sanitize_env(env: dict) -> dict:
    cleaned = {}
    for k, v in env.items():
        upper = k.upper()
        if any(s in upper for s in _SENSITIVE_ENV_KEYS):
            continue
        cleaned[k] = v
    return cleaned


# =========================================================
# Shell 执行（Linux，含安全策略）
# =========================================================
def execute_shell(command: str, cwd: str = None, timeout: int = None):
    if not CFG.allow_shell:
        raise SandboxError("shell 执行已被禁用")

    _check_shell_safety(command)

    if CFG.shell_whitelist:
        stripped = command.strip()
        if not stripped:
            raise SandboxError("空命令")
        first_word = stripped.split()[0]
        if first_word not in CFG.shell_whitelist:
            raise SandboxError(f"命令 {first_word} 不在白名单中")

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
            env=_sanitize_env(os.environ.copy()),
        )
        ok = proc.returncode == 0
        _audit("execute_shell", {"command": command, "cwd": cwd}, ok,
               f"exit={proc.returncode}")
        return (f"exit_code:{proc.returncode}, "
                f"stdout:{_clip(proc.stdout)}, "
                f"stderr:{_clip(proc.stderr)}\n")
    except subprocess.TimeoutExpired:
        _audit("execute_shell", {"command": command}, False, "timeout")
        raise SandboxError(f"命令超时 (>{timeout}s)")


def python_exec(code: str, timeout: int = None):
    if not CFG.allow_python:
        raise SandboxError("python 执行已被禁用")

    timeout = min(timeout or CFG.default_timeout, CFG.max_timeout)

    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(CFG.root),
            env=_sanitize_env(os.environ.copy()),
        )
        ok = proc.returncode == 0
        _audit("python_exec", {"code_len": len(code)}, ok,
               f"exit={proc.returncode}")
        return (f"exit_code:{proc.returncode}, "
                f"stdout:{_clip(proc.stdout)}, "
                f"stderr:{_clip(proc.stderr)}\n")

    except subprocess.TimeoutExpired:
        _audit("python_exec", {"code_len": len(code)}, False, "timeout")
        raise SandboxError(f"Python 执行超时 (>{timeout}s)")


# =========================================================
# 网络（无主机白名单）
# =========================================================
def http_request(
        url: str,
        method: str = "GET",
        headers: dict = None,
        params: dict = None,
        json_body: dict = None,
        data: str = None,
        timeout: int = 30,
):
    if not CFG.allow_network:
        raise SandboxError("网络访问已被禁用")

    if CFG.allowed_hosts is not None:
        from urllib.parse import urlparse
        _host = urlparse(url).hostname or ""
        if not any(_host == h or _host.endswith("." + h) for h in CFG.allowed_hosts):
            raise SandboxError(f"主机 {_host} 不在白名单中")

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
    """返回运行环境的基本系统信息，以及 CFG.root 的 Git 状态（如果适用）。"""
    import platform

    info = {
        "os_system": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "python_version": sys.version.split()[0],
        "sandbox_root": str(CFG.root),
        "read_only_mode": CFG.read_only,
    }

    if CFG.allow_shell:
        try:
            rev_parse = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(CFG.root),
                capture_output=True,
                text=True,
                timeout=3,
            )
            if rev_parse.returncode == 0 and rev_parse.stdout.strip() == "true":
                info["is_git_repo"] = True

                branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=str(CFG.root),
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if branch.returncode == 0:
                    info["git_branch"] = branch.stdout.strip() or "detached HEAD"

                commit = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=str(CFG.root),
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if commit.returncode == 0:
                    info["git_commit"] = commit.stdout.strip()
            else:
                info["is_git_repo"] = False
                info["git_status"] = "not_a_git_repo"
        except FileNotFoundError:
            info["git_status"] = "git_not_installed"
        except Exception:
            info["git_status"] = "check_failed"
    else:
        info["git_status"] = "shell_execution_disabled"

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
        try:
            now = datetime.now(ZoneInfo(timezone))
        except Exception:
            now = datetime.now()
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
    return CFG.root / "agent_memory.txt"


def memory_save(value: str):
    one_line = " ".join(str(value).splitlines())
    p = _memory_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    with CFG._lock:
        with p.open("a", encoding="utf-8", newline="\n") as f:
            print(one_line, file=f)
    _audit("memory_save", {"value_len": len(one_line)}, True)
    return {"ok": True, "value": one_line}


def memory_recall():
    p = _memory_file()
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    lines2 = [ln for ln in text.splitlines() if ln.strip()]
    return {"count": len(lines2), "lines": lines2}


# =========================================================
# 工具注册（schema + 分发）
# =========================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
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
            "description": "写入文件，可追加。",
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
            "description": "列出目录。",
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
            "description": "删除文件。",
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
            "description": "删除目录（可递归）。",
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
            "description": "按 glob 搜索文件。",
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
            "description": "按正则搜索文件内容。",
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
            "description": "执行 shell 命令。",
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
            "description": "执行 Python 代码。",
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
            "description": "发起 HTTP 请求。",
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
            "description": "保存一条记忆（追加一行到 agent_memory.txt）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                },
                "required": ["value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_recall",
            "description": "读取全部记忆（逐行返回）。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_info",
            "description": "获取运行环境的基本系统信息，以及工作目录的 Git 仓库状态。",
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
        result = {"error": f"执行拒绝: {e}"}
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}

    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)