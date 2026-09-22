# Console 实时日志采集

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Manifest V3](https://img.shields.io/badge/Chrome-Manifest%20V3-4285F4.svg)](https://developer.chrome.com/docs/extensions/mv3)

Chrome 侧边栏扩展：采集当前标签页的 `console` 输出，实时显示，并写入本地文件。

适合网页游戏、Cocos 预览页这类**每秒几十到上百条日志**的场景。开着 DevTools Console 会把对象和调用栈留在内存里，页面容易卡死或崩溃；本扩展不打开 DevTools，采集和写文件尽量不占游戏主线程。

## 能做什么

- 点工具栏图标打开**侧边栏**（不是一失焦就关的小弹窗）
- 按标签页开关采集，锁定后切走其它页也不串日志
- 级别筛选、搜索、重复合并、错误跳转、错误汇总文件
- 默认**静音原生 console**，侧边栏默认只显示 Warnings / Errors；勾上 Info 就会开始实时刷 Info
- 完整日志写入你选的文件夹（采集中先追加到浏览器 OPFS，点「同步到目录」或「停止」再落到硬盘）
- 写文件在独立 Worker 里做，避免磁盘尖峰卡住页面

## 安装

1. 克隆本仓库，或下载 ZIP 后解压  
   `git clone https://github.com/H-M-Master/console-log-capture.git`
2. Chrome 打开 `chrome://extensions`
3. 打开右上角「开发者模式」
4. 「加载已解压的扩展程序」，选仓库里的 **`console-extension`** 文件夹（不要选仓库根目录）

加载成功后，工具栏会出现「Console 实时日志采集」。

## 使用

1. 打开要监控的页面（例如 Cocos 预览 `http://localhost:7456/`），保持该标签页为当前页
2. 点插件图标，打开右侧侧边栏
3. 点「开始」，第一次会让你选一个保存日志的文件夹（之后会记住）
4. 玩游戏或操作页面；错误和警告会实时出现在侧边栏
5. 需要看 Info 时，在「全部级别」里勾上 **Info**；不看了再取消，避免拖角色时刷屏卡顿
6. 点「停止」结束采集。也可点「同步到目录」把当前日志立刻写到所选文件夹

重新加载或更新扩展后，请**刷新目标页面**再点「开始」，否则页面里还是旧的注入脚本。

## 生成的文件

都在你选的那个目录里：

| 文件 | 内容 |
| --- | --- |
| `console-log-<时间>.txt` | 完整日志（不限行数） |
| `console-log-<时间>-errors.txt` | 仅 Errors，方便发给开发 |
| `console-capture-diag-<时间>.jsonl` | 自检指标（速率、写入耗时、自动降级记录） |

侧边栏最多显示最近 2000 行，只影响界面，不影响磁盘上的完整文件。

## 建议默认设置

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| 静音（不动原生 console） | 开 | 拖角色等高频日志时，再走一遍浏览器 console 很容易卡 |
| 级别筛选 | 仅 Warnings / Errors | 需要 Info 时再勾；完整内容仍在 txt 里 |
| 完全不刷侧边栏 | 关 | 极限性能才开；开了就看不到实时错误 |

保存目录不要选在 Cocos 工程内部，否则写文件可能触发编辑器重新编译。

## 自动降级

插件会自己采样。日志暴涨或写入过慢时，会自动静音、收紧实时级别、降低写盘频率，并记进诊断文件。页面被游戏热重载后，会尝试重新注入采集脚本。

## 目录结构

```
console-extension/     ← 加载扩展时选这个文件夹
  manifest.json
  background.js
  sidepanel.html
  sidepanel.js
  write-worker.js
  diagnostics.js
  icons/
LICENSE
```

## License

[MIT](LICENSE)
