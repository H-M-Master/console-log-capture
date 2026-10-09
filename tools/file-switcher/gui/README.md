# 图形化文件版本切换器

这是 `file-switcher` 的 tkinter 图形界面，使用 Python 3.10 标准库，不需要安装第三方依赖。

## 启动

直接双击：

```text
run.cmd
```

也可以使用命令行：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe" .\app.py
```

## 功能

- 状态首页：当前模式、文件数量、短 SHA-256、Creator 进程和缓存状态；
- 一键切换：动态读取配置中的模式，支持预演和确认后正式切换；
- 文件与模式：编辑 `config.json`，可配置任意数量、任意扩展名的目标文件；
- 日志与事务：查看 PowerShell 输出以及 `state` 下的事务 journal；
- 配置保存前进行基础校验，并保留 `config.json.bak`。

GUI 不直接复制、移动、删除目标文件、缓存或快照。所有实际切换仍由上级目录的 `switch-files.ps1` 执行，保留其哈希校验、备份、缓存隔离和失败回滚逻辑。

正式切换前请关闭 Cocos Creator。默认勾选“先预演”；确认无误后取消勾选再执行切换。
