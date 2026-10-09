# 通用双版本文件切换器

这是一个可配置的 PowerShell 文件切换工具。它不限定文件数量：在 `config.json` 的 `entries` 数组中增删条目即可。

## 当前配置

当前已经录入两种模式：

- `daily`：日常测试版
- `automation`：自动化版

当前示例包含：

- Cocos `gfx/webgl/webgl-commands.ts`
- Cocos `rendering/custom/executor.ts`

快照保存在 `snapshots/daily` 和 `snapshots/automation`。脚本会按 SHA-256 校验快照和切换后的目标文件。

## 使用

建议先关闭 Cocos Creator，然后在此目录打开 PowerShell：

```powershell
# 查看当前模式，不修改任何文件
.\switch-files.ps1 -Status

# 只检查配置和将要替换的文件，不落盘
.\switch-files.ps1 -Mode automation -DryRun

# 切换到自动化版
.\switch-files.ps1 -Mode automation

# 切换回日常测试版
.\switch-files.ps1 -Mode daily
```

也可以双击或在命令行运行 `switch-files.cmd`，例如：

```cmd
switch-files.cmd -Status
switch-files.cmd -Mode automation -DryRun
switch-files.cmd -Mode daily
```

切换过程会：

1. 检查全部源文件、目标文件和配置；
2. 检查 Cocos Creator 是否关闭；
3. 识别当前文件是否完全匹配某个模式；
4. 创建事务备份；
5. 隔离已发现的 Cocos transform-cache 目录，不直接删除；
6. 对每个输入文件先写临时文件并校验，再替换目标；
7. 校验所有目标文件，失败时尝试回滚。

脚本不会自动启动或结束 Cocos Creator。成功后重新启动 Creator，让它重新生成缓存。

## 如何增删文件

编辑 `config.json` 的 `entries`。每个条目包含目标相对路径和每个模式的源快照路径：

```json
{
  "target": "相对于 targetRoot 的文件路径",
  "sources": {
    "daily": "相对于本工具目录的日常版快照路径",
    "automation": "相对于本工具目录的自动化版快照路径"
  }
}
```

增加第三种模式时，同时在 `modes` 中增加名称，并在**每一个** entry 的 `sources` 中提供该名称对应的文件。模式名区分大小写。所有条目的目标路径必须唯一，并且不能越出 `targetRoot`。

增加文件的推荐流程：

1. 在两个快照目录中按目标相对路径放入两套文件；
2. 在 `entries` 中新增映射；
3. 运行 `-Status` 和 `-DryRun`；
4. 确认输出后再正式切换。

## 事务状态

切换产生的备份、日志和缓存隔离目录在 `state/<transaction-id>` 下。不要手动删除正在使用的事务目录；如出现错误，先查看其中的 `journal.json` 和备份，再人工处理。

当前工具默认拒绝“混合模式”或未知文件集合。确认目标文件确实可以被配置快照覆盖时，才使用：

```powershell
.\switch-files.ps1 -Mode automation -AllowUnknown
```

`-AllowUnknown` 只放宽当前状态检查，不跳过源文件哈希校验。
