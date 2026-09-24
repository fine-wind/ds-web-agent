# DeepSeek Agent 文件操作工具

## 项目简介
本项目基于 Tampermonkey 用户脚本和 Flask 后端服务，实现 DeepSeek 对话中的文件操作自动化。AI 回复中的 JSON 执行计划会被解析并调用本地文件服务完成创建、读取、覆盖、追加、替换、删除、列出等操作。

## 功能特性
- 自动监听 DeepSeek AI 回复并解析执行计划
- 支持 7 种文件操作：create、read、overwrite、append、replace、delete、list
- 前端悬浮按钮（可拖动），一键启动/停止，带重置输入框
- 路径校验、符号链接保护、日志轮转等安全措施


```text
用户 → 网页 AI → 输出操作命令文本 → JS 捕获
                                  ↓
                           WebSocket 发给 py
                                  ↓
                        py 把这段文本发给本地 LLM
                                  ↓
                        本地 LLM 推理 → 返回 tool_calls
                                  ↓
                        py 执行工具 → 结果通过 WebSocket 返回给 JS
                                  ↓
                        JS 把结果发回网页 AI
                                  ↓
                        网页 AI 继续推理...
```
