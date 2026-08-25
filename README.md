# DeepSeek Agent 文件操作工具

## 项目简介
本项目基于 Tampermonkey 用户脚本和 Flask 后端服务，实现 DeepSeek 对话中的文件操作自动化。AI 回复中的 JSON 执行计划会被解析并调用本地文件服务完成创建、读取、覆盖、追加、替换、删除、列出等操作。

## 功能特性
- 自动监听 DeepSeek AI 回复并解析执行计划
- 支持 7 种文件操作：create、read、overwrite、append、replace、delete、list
- 前端悬浮按钮（可拖动），一键启动/停止，带重置输入框
- 路径校验、符号链接保护、日志轮转等安全措施

## 快速开始

### 后端
bash
pip install flask flask-cors
python agent_executor.py

默认监听 0.0.0.0:8888，可通过环境变量 AGENT_HOST、AGENT_PORT、AGENT_WORK_DIR、AGENT_CORS_ORIGINS 配置。

### 前端
1. 安装 Tampermonkey 扩展
2. 新建脚本，粘贴 Tampermonkey.js 内容
3. 打开 chat.deepseek.com，右下角出现悬浮按钮

## 使用说明
点击 Agent 按钮启动，AI 回复中包含 JSON 执行计划时自动调用文件接口。

执行计划示例：
json
{
"task": "创建文件",
"type": "file",
"action": "create",
"path": "test.txt",
"content": "Hello"
}


## API 接口
- GET /health 健康检查
- GET /prompt 获取提示词
- POST /file 文件操作

详见代码注释。
