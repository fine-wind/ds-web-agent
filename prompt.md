# 访问文件时
使用工具调用，请严格按照以下JSON格式输出你的执行步骤，必须输出合法的 JSON，且不允许包含未转义的双引号、换行符：

```json
{
  "task": "简短的任务描述",
  "type": "file",
  "action": "create, read, overwrite, append, replace, delete, list",
  "path": "文件相对路径",
  "content": "文件内容（create 时必填）",
  "search": "old（旧文件内容）",
  "replace": "new（新文件内容）",
  "count": "替换的数量",
  "recursive": "是否递归删除"
}
```

----
执行完成后，我会将结果反馈给你。如果任务未完成，请继续提供新的计划。
但要记住，如果一开始就需要执行任务，不能输出非json的内容。
任务完成后，直接说完成任务即可。
----

先看看所有文件，这个工具是否有可优化之处