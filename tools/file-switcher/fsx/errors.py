"""Backend error codes, translated for people who don't read stack traces.

``folder-switcher.ps1`` reports a machine-readable ``error.code``.  The raw
English message is still there and still goes to the log, but nothing in the
GUI shows it: this module turns the code into a headline, a sentence of plain
Chinese, and — the part that actually unblocks someone — a next step.

The code list is mirrored by ``tests/test_backend_contract.py`` (which proves
the script really emits each code) and by ``docs/技术文档.md`` (which documents
them).  Adding a code to the script without adding it here fails a test.
"""
from __future__ import annotations

from typing import Any

# code -> (headline, what happened, what to do next)
EXPLANATIONS: dict[str, tuple[str, str, str]] = {
    # --- something outside the tool is in the way -------------------------
    "guard-process-running": (
        "有程序正在使用这些文件",
        "目标目录里的文件正被另一个程序打开，现在替换会失败或损坏文件。",
        "请关闭占用的程序（通常是游戏或编辑器）后重试。"),
    "operation-running": (
        "已经有一个操作在进行",
        "上一次操作还没结束，同时运行两个会互相破坏。",
        "等当前操作结束后再试一次。"),
    "transaction-incomplete": (
        "上一次切换没有完成",
        "上次切换中途中断了，需要先把它收拾干净才能继续。",
        "到“高级管理 → 日志与事务”里选中那条事务，点“预览并回滚”。"),

    # --- the thing you asked for does not exist yet -----------------------
    "state-not-captured": (
        "这个版本还没有保存过内容",
        "选中版本还没有存下任何文件，没法切换过去。",
        "先到“设置”里为它保存一次当前内容（采集）。"),
    "manifest-not-found": (
        "这个版本保存的内容不见了",
        "版本记录还在，但对应的存档文件找不到了。",
        "重新为这个版本采集一次。"),
    "profile-not-found": (
        "找不到这个设置",
        "配置里没有你要的那个设置项，可能是配置被改动过。",
        "点“重新加载”，或在高级管理里重新建立。"),
    "state-not-found": (
        "找不到这个版本",
        "配置里没有你要的那个版本，可能是配置被改动过。",
        "点“重新加载”后重新选择版本。"),
    "state-id-required": (
        "还没有选择版本",
        "这个操作需要先指定要切换到哪个版本。",
        "先在列表里选择一个版本再操作。"),
    "config-not-found": (
        "还没有设置过",
        "还没建立任何版本设置。",
        "点“设置 / 添加版本”按向导走一遍。"),
    "transaction-not-found": (
        "找不到这条事务记录",
        "事务日志文件不存在，可能已被清理。",
        "点“刷新事务”重新读取列表。"),
    "transaction-already-rolled-back": (
        "这条事务已经回滚过了",
        "它记录的那些修改早已撤销，不需要再来一次。",
        "刷新事务列表，选择别的事务。"),

    # --- refusing to overwrite content it does not recognize --------------
    "plan-target-conflict": (
        "有文件被别的程序改过",
        "目标目录里有些文件的内容和工具记录的不一样，直接切换会覆盖掉别人改的东西。",
        "先到“高级管理”看一下差异；如果确认这些改动可以丢弃，勾选“允许未知覆盖”后再试。"),
    "plan-delete-unauthorized": (
        "这次切换会删掉文件",
        "按这个版本的内容，有些文件需要被删除，工具默认不删。",
        "确认可以删除后，勾选“允许受管删除”再试。"),
    "plan-delete-unrecognized": (
        "要删的文件内容不认识",
        "准备删除的文件里，有些内容任何版本都没记录过，工具不敢删。",
        "如果那些内容还需要，先为它采集一次保存下来；确实不要了再逐项确认。"),
    "plan-blocked": (
        "计划被安全检查拦住了",
        "切换前的检查发现了风险，已经停下来没有动文件。",
        "查看下方的阻止原因，按提示处理后重新生成预览。"),
    "plan-stale-digest": (
        "文件在确认之后又被改动了",
        "从你看到预览到你点确认之间，目标目录发生了变化。",
        "重新生成一次预览，确认后再应用。"),
    "plan-path-escape": (
        "文件路径超出了目标目录",
        "某个受管文件的路径指向了目标目录外面，出于安全已拒绝。",
        "到“扫描与受管文件”里检查并移除那条路径。"),
    "digest-required": (
        "需要先生成预览",
        "应用前必须先用一次预览确认要改哪些文件。",
        "点“生成差异预览”，确认后再应用。"),

    # --- saved content failed its integrity check -------------------------
    "snapshot-hash-mismatch": (
        "保存的内容校验不通过",
        "存档里的文件内容对不上，可能已损坏。",
        "重新为这个版本采集一次。"),
    "backup-hash-mismatch": (
        "备份文件校验不通过",
        "用于回滚的备份内容对不上，可能已损坏。",
        "不要继续回滚，请手动检查目标目录。"),
    "backup-missing": (
        "备份文件不见了",
        "回滚需要的备份文件已被删除或移动。",
        "不要继续回滚，请手动检查目标目录。"),
    "staged-hash-mismatch": (
        "写入前的临时文件校验不通过",
        "准备写入的内容在暂存时就对不上，已经中止。",
        "重试一次；若仍失败，重新采集该版本。"),
    "rollback-guard": (
        "文件被改过，无法安全回滚",
        "回滚会覆盖掉切换之后别人做的修改。",
        "确认可以丢弃这些修改后再手动处理。"),

    # --- the configuration itself is wrong --------------------------------
    "config-schema": (
        "设置文件的格式版本不对",
        "这份配置不是本工具现在使用的格式。",
        "在高级管理里重新建立设置。"),
    "config-empty": (
        "设置文件里没有任何内容",
        "配置存在但没有可用的版本设置。",
        "点“设置 / 添加版本”重新建立。"),
    "config-profile-id": (
        "设置文件的标识有重复",
        "配置里有重复或空的标识。",
        "在高级管理里检查并删除重复项。"),
    "config-state-id": (
        "版本标识有重复",
        "同一个设置下有重复或空的版本标识。",
        "在高级管理里检查并删除重复项。"),
    "config-default-profile": (
        "默认设置指向了不存在的项",
        "配置记录的默认设置已经不存在了。",
        "点“重新加载”，必要时重建设置。"),
    "config-target-overlap": (
        "目标目录和工具的数据目录重叠",
        "两者套在一起会让工具把自己的存档当成待替换的文件。",
        "换一个目标目录，或在高级管理里修改数据目录。"),
    "config-target-drive-root": (
        "不能把整个磁盘当作目标目录",
        "目标目录不能是盘符根目录。",
        "请选择具体的文件夹。"),
    "config-target-system-root": (
        "不能把系统目录当作目标目录",
        "目标目录不能是 Windows 或 Program Files 这类系统位置。",
        "请选择你自己的项目或游戏目录。"),
    "config-targets-overlap": (
        "两个设置的目标目录重叠",
        "同一个目录被两个设置同时管理，会互相冲突。",
        "在高级管理里改为互不包含的目录。"),
    "config-managed-duplicate": (
        "同一个文件被重复管理",
        "受管清单里有重复的路径。",
        "在高级管理里删除重复项。"),
    "config-managed-directory": (
        "受管清单里填了文件夹",
        "只能管理单个文件，不能管理整个文件夹。",
        "改为选择具体的文件。"),
    "config-cache-outside-target": (
        "缓存目录不在目标目录里",
        "配置的缓存目录必须在目标目录之内。",
        "在高级管理里修正缓存目录。"),
    "config-snapshot-outside-state": (
        "存档位置超出了数据目录",
        "版本的存档目录必须在工具的数据目录之内。",
        "删除该版本后重新采集。"),
    "snapshot-outside-state": (
        "存档位置超出了数据目录",
        "版本的存档目录必须在工具的数据目录之内。",
        "删除该版本后重新采集。"),
    "source-inside-state": (
        "来源目录不能是工具的数据目录",
        "从工具的存档目录里再采集一次会造成循环。",
        "选择真正的项目或游戏目录。"),
    "source-path-directory": (
        "来源里该是文件的地方是文件夹",
        "采集来源里某个受管路径实际是个目录。",
        "检查来源目录的结构，或取消管理该路径。"),

    # --- path safety ------------------------------------------------------
    "path-unsafe-relative": (
        "文件路径不安全",
        "受管路径必须是目标目录内的相对路径，不能含 ../ 或盘符。",
        "在“扫描与受管文件”里移除这条路径，重新选择文件。"),
    "path-reserved": (
        "文件名是系统保留名",
        "CON、NUL 这类名字在 Windows 上不能作为文件名。",
        "请换一个文件名。"),
    "path-device": (
        "路径指向了系统设备",
        "这类路径不是普通文件，不能参与切换。",
        "请选择普通文件夹内的文件。"),
    "path-reparse": (
        "路径是符号链接或快捷方式",
        "链接可能指向目录之外，工具拒绝操作。",
        "请选择真实目录，不要使用链接。"),
    "path-not-found": (
        "路径不存在",
        "要操作的目录或文件不在原位置了。",
        "确认目录没有被移动或重命名。"),
    "path-empty": (
        "路径是空的",
        "有一处路径没有填写。",
        "在高级管理里补全或删除那一项。"),
    "target-not-regular-file": (
        "目标不是普通文件",
        "该位置存在但不是普通文件，无法替换。",
        "检查目标目录，移除同名文件夹或链接。"),

    # --- value/shape problems ---------------------------------------------
    "value-not-boolean": (
        "配置里的开关值无效",
        "某个布尔字段填了无法识别的值。",
        "在高级管理里修正该配置项。"),
    "manifest-managed-mismatch": (
        "存档和当前的受管清单不一致",
        "受管文件列表改动过，旧存档不再适用。",
        "重新为这个版本采集一次。"),
    "manifest-identity": (
        "存档文件与版本对不上",
        "存档里的标识和当前设置不匹配。",
        "重新为这个版本采集一次。"),
    "manifest-coverage": (
        "存档缺少部分受管文件",
        "存档没有覆盖全部受管文件，不能安全使用。",
        "重新为这个版本采集一次。"),
    "manifest-path-duplicate": (
        "存档里有重复的文件记录",
        "存档文件本身有问题。",
        "重新为这个版本采集一次。"),
    "manifest-hash": (
        "存档里的文件哈希无效",
        "存档记录的内容不完整或已损坏。",
        "重新为这个版本采集一次。"),
    "manifest-size": (
        "存档里的文件大小无效",
        "存档记录的内容不完整或已损坏。",
        "重新为这个版本采集一次。"),
    "transaction-id-invalid": (
        "事务编号无效",
        "事务编号必须是简单的名字，不能包含路径符号。",
        "在列表里重新选择一条事务。"),

    # --- protocol/transport level, reported by fsx.backend ----------------
    "backend-no-output": (
        "后端没有返回结果",
        "工具调用 PowerShell 执行操作，但没有拿到可读的结果。",
        "重试一次；若反复出现，把日志发给维护者。"),
    "backend-bad-json": (
        "后端返回的结果无法解析",
        "收到的内容和约定的格式不符，可能是脚本版本不匹配。",
        "确认脚本与程序来自同一次发布。"),
    "backend-contract": (
        "后端返回的结果不符合约定",
        "收到的内容缺少必需字段，无法安全继续。",
        "确认脚本与程序来自同一次发布。"),
    "backend-exit-mismatch": (
        "后端的结果自相矛盾",
        "返回的成功标记和退出码不一致，出于安全没有采用。",
        "重试一次；若反复出现，把日志发给维护者。"),
    "backend-timeout": (
        "操作超时",
        "读取状态的时间超过了预期。",
        "稍后重试；目录很大时首次读取会比较慢。"),
    "backend-start-failed": (
        "无法启动后端",
        "调用 PowerShell 失败，可能是环境限制。",
        "确认系统允许运行 PowerShell 脚本。"),

    "operation-failed": (
        "操作失败",
        "操作没有完成，具体原因见下方详细信息。",
        "重试一次；若仍失败，请把详细信息发给维护者。"),
}

FALLBACK = (
    "操作失败",
    "操作没有完成。",
    "请重试一次；若仍失败，把详细信息发给维护者。",
)


def explain(payload: dict[str, Any] | None) -> tuple[str, str, str, str]:
    """Translate a backend result into ``(headline, reason, next step, detail)``.

    ``payload`` is the parsed backend JSON.  Unknown codes fall back to a
    generic explanation rather than leaking the raw English message, but the
    detail string always carries the original text so logs stay diagnosable.
    """
    error = (payload or {}).get("error") or {}
    code = str(error.get("code") or "").strip()
    raw = str(error.get("message") or "").strip()

    headline, reason, action = EXPLANATIONS.get(code, FALLBACK)
    detail_parts = []
    if code:
        detail_parts.append(f"错误代码：{code}")
    if raw:
        detail_parts.append(f"后端原文：{raw}")
    return headline, reason, action, "\n".join(detail_parts)


def known_codes() -> set[str]:
    """Every code this module can explain; used to keep docs and tests honest."""
    return set(EXPLANATIONS)
