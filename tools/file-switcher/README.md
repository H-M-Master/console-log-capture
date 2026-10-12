# 文件版本切换器

在你保存过的几个版本之间一键切换文件。**只有你勾选管理的文件会被替换，目录里其它文件一律不动。**

版本名字由你自己起，界面上不出现任何技术名词。

## 快速开始

双击运行：

```text
dist\FolderSwitcher.exe
```

首次打开会引导你建立第一个版本：

1. **选择文件夹** —— 要切换文件的那个目录（游戏目录或工程目录）。
2. **勾选文件** —— 只有勾选的会跟着版本切换，没勾的永远不会被改动。
3. **起名字并保存内容** —— 例如"正式版""测试版"，并把当前内容保存下来。

之后首页就会出现版本按钮：**点一下 → 确认 → 切换完成**。

想再加一个版本，再走一次这三步即可。

## 日常使用

首页上：

- 顶部显示**当前是哪个版本**。
- 中间每个版本一个大按钮，点它就能切过去。
- **撤销上一次切换**把文件还原成上一次切换之前的样子。
- **高级管理**里是完整功能（多套设置、逐个文件管理、比较明细、历史记录）。

## 数据存在哪

配置、版本内容、备份和日志**全部只保存在本机**：

```text
%LOCALAPPDATA%\AnyTestTools\FolderSwitcher\
```

程序不联网、不上传任何数据。详见 [PRIVACY.md](PRIVACY.md)。

想把它放到别处（例如便携 U 盘），设置环境变量 `ANYTESTTOOLS_DATA_ROOT` 指向目标目录即可。

## 安全设计

切换文件是会动你东西的操作，所以它的默认行为是保守的：

- **只碰你勾选的文件。** 目录里其它文件在任何操作中都不会被读写。
- **先看清楚再动手。** 每次切换都先算出"会改哪些文件"，确认后才真正写入；
  如果你看到预览之后文件又被别的程序改了，工具会拒绝执行并让你重新预览。
- **有备无患。** 写入前先备份原文件，出错时自动回滚；每次切换都留下记录，可以事后撤销。
- **不认识的内容不覆盖。** 如果目标文件的内容不属于任何已保存版本，工具默认拒绝覆盖，
  需要你在高级模式里明确允许。
- **占用中的程序会被检测。** 目标程序（如 Cocos Creator）正在运行时会提示你先关闭。

## 命令行用法

高级模式下全部功能都由 `folder-switcher.ps1` 提供，可直接脚本调用：

```powershell
# 查看当前状态，不改任何文件
.\folder-switcher.ps1 -Action Status

# 预览会改哪些文件
.\folder-switcher.ps1 -Action Diff -StateId <版本id>

# 应用（必须带上一步的 planDigest）
.\folder-switcher.ps1 -Action Apply -StateId <版本id> -ExpectedPlanDigest <digest>

# 撤销某次切换
.\folder-switcher.ps1 -Action Rollback -TransactionId <记录id>
```

失败时返回单行 JSON，其中 `error.code` 是稳定的错误码（详见
[docs/技术文档.md](docs/技术文档.md) 的错误码表），`error.message` 保留英文原文供排查。

## 开发

```powershell
# 运行测试（需要 Python 3.10 与 PowerShell 5.1）
python -m unittest discover -s tests -t tests

# 从源码直接运行
python folder_app.py

# 打包成单个 exe
pyinstaller --clean --noconfirm FolderSwitcher-onefile.spec
```

代码分层：`folder_app.py` 只是入口，实际实现在 `fsx/` 包里
（`paths` / `errors` / `backend` / `store` / `model` / `ui`）。
想了解协议与错误码，看 [docs/技术文档.md](docs/技术文档.md)；
想了解使用者要什么，看 [docs/需求文档.md](docs/需求文档.md)。
