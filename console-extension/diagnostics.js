// 自动诊断器：插件自己采集运行指标、自己判断异常、并在情况严重时自动降级，
// 不需要用户手动观察和汇报。
//
// 采集的指标（每秒一次）：
//   - 页面侧：每条日志的采集耗时（µs）、原生 console 占比、缓冲积压量
//   - 侧边栏侧：每秒日志条数、视图 DOM 节点数、渲染单次耗时、写文件耗时
//   - 环境：JS 堆大小、标签页是否可见
//
// 判断规则（满足即写入诊断文件并升级对策）：
//   - 采集耗时 > 阈值            → 说明采集路径本身贵，收紧参数
//   - 原生 console 占比 > 阈值   → 说明是原生输出在吃 CPU，自动切静音
//   - DOM 节点数 > 上限          → 视图出问题，强制重建
//   - 堆增长异常                 → 记下来
//
// 诊断文件默认写到「下载目录」而不是用户选的日志目录，
// 避免因为写文件触发 Cocos 等开发服务器的重新编译。

const DIAG_FILE_PREFIX = 'console-capture-diag';

// 阈值：超过就认为异常
const THRESHOLDS = {
  perLineUs: 200, // 每条日志采集超过 200µs 就值得警惕
  nativePercent: 20, // 原生 console 占主线程超过 20% 说明它在吃 CPU
  domNodes: 4000, // 视图 DOM 节点数上限（正常应 <= 2000）
  backlog: 1500, // 页面侧缓冲积压超过这个数说明搬运跟不上
  writePercent: 15, // 落盘占每秒时间超过 15% 说明写得太频繁
};

/**
 * 指标聚合器：收集一段时间窗口内的样本，产出结论。
 */
class MetricsCollector {
  constructor() {
    this.reset();
  }

  reset() {
    this.window = {
      lines: 0,
      captureMs: 0,
      nativeMs: 0,
      flushMs: 0,
      writeMs: 0,
      writeCalls: 0,
      writeBytes: 0,
      maxWriteMs: 0,
      renderMs: 0,
      renderCount: 0,
      domNodes: 0,
      backlog: 0,
      heapMB: 0,
      visible: true,
      startedAt: Date.now(),
    };
    this.writeCallSamples = [];
    this.samples = [];
  }

  // 页面侧上报
  addPageStats(s) {
    if (!s) return;
    this.window.lines += s.lines || 0;
    this.window.captureMs += s.describeMs || 0;
    this.window.flushMs += s.flushMs || 0;
    this.window.nativeMs += s.nativeMs || 0;
    if (typeof s.backlog === 'number') this.window.backlog = s.backlog;
  }

  // 侧边栏侧记录
  addRender(ms, domNodes) {
    this.window.renderMs += ms;
    this.window.renderCount++;
    this.window.domNodes = domNodes;
  }

  addWrite(ms) {
    this.window.writeMs += ms;
  }

  // 记录一次写入调用的开销，用来区分"调用次数"和"数据量"哪个才是成本来源
  addWriteCall(ms, bytes) {
    this.window.writeCalls++;
    this.window.writeBytes += bytes || 0;
    if (ms > this.window.maxWriteMs) this.window.maxWriteMs = ms;
    this.writeCallSamples.push({ ms: Number(ms.toFixed(1)), bytes });
    if (this.writeCallSamples.length > 100) this.writeCallSamples.shift();
  }

  setHeap(mb) {
    this.window.heapMB = mb;
  }

  setVisible(v) {
    this.window.visible = v;
  }

  /** 把当前窗口结算成一个样本，并重置窗口 */
  flush() {
    const w = this.window;
    const elapsedSec = Math.max(0.001, (Date.now() - w.startedAt) / 1000);
    const totalMs = w.captureMs + w.flushMs + w.nativeMs + w.writeMs + w.renderMs;

    const sample = {
      at: new Date(w.startedAt).toISOString(),
      sec: Number(elapsedSec.toFixed(2)),
      lines: w.lines,
      rate: Number((w.lines / elapsedSec).toFixed(1)),
      captureMs: Number(w.captureMs.toFixed(2)),
      nativeMs: Number(w.nativeMs.toFixed(2)),
      flushMs: Number(w.flushMs.toFixed(2)),
      writeMs: Number(w.writeMs.toFixed(2)),
      writeCalls: w.writeCalls,
      writeBytes: w.writeBytes,
      maxWriteMs: Number(w.maxWriteMs.toFixed(1)),
      // 单次写入调用平均耗时：用来判断成本是否集中在"调用次数"上
      perWriteMs: w.writeCalls > 0 ? Number((w.writeMs / w.writeCalls).toFixed(1)) : 0,
      renderMs: Number(w.renderMs.toFixed(2)),
      renderCount: w.renderCount,
      domNodes: w.domNodes,
      backlog: w.backlog,
      heapMB: Number(w.heapMB.toFixed(1)),
      visible: w.visible,
      perLineUs: w.lines > 0 ? Number(((w.captureMs / w.lines) * 1000).toFixed(2)) : 0,
      nativePct: Number(((w.nativeMs / (elapsedSec * 1000)) * 100).toFixed(2)),
      pluginPct: Number((((w.captureMs + w.flushMs) / (elapsedSec * 1000)) * 100).toFixed(3)),
      totalPct: Number(((totalMs / (elapsedSec * 1000)) * 100).toFixed(2)),
    };

    this.samples.push(sample);
    if (this.samples.length > 600) this.samples.shift(); // 最多留 10 分钟
    this.resetWindow();
    return sample;
  }

  resetWindow() {
    const visible = this.window.visible;
    this.window = {
      lines: 0,
      captureMs: 0,
      nativeMs: 0,
      flushMs: 0,
      writeMs: 0,
      writeCalls: 0,
      writeBytes: 0,
      maxWriteMs: 0,
      renderMs: 0,
      renderCount: 0,
      domNodes: this.window.domNodes,
      backlog: 0,
      heapMB: this.window.heapMB,
      visible,
      startedAt: Date.now(),
    };
  }

  /**
   * 根据最近若干样本判断是否需要干预。
   * 返回一个结论对象，交由调用方决定怎么降级。
   */
  diagnose(recentCount = 5) {
    const recent = this.samples.slice(-recentCount);
    if (recent.length === 0) return { level: 'ok', reasons: [] };

    const reasons = [];
    const avg = (key) => recent.reduce((a, s) => a + s[key], 0) / recent.length;

    const avgPerLine = avg('perLineUs');
    const avgNativePct = avg('nativePct');
    const avgPluginPct = avg('pluginPct');
    const avgWriteMs = avg('writeMs');
    const avgWritePct = (avgWriteMs / 1000) * 100; // 每秒写入耗时（样本窗口为 1 秒）
    const avgPerWriteMs = avg('perWriteMs');
    const avgWriteCalls = avg('writeCalls');
    const maxDom = Math.max(...recent.map((s) => s.domNodes));
    const maxBacklog = Math.max(...recent.map((s) => s.backlog));
    const avgRate = avg('rate');
    // 堆增长：后半段平均相对前半段
    const half = Math.max(1, Math.floor(recent.length / 2));
    const firstHalf = recent.slice(0, half).reduce((a, s) => a + s.heapMB, 0) / half;
    const secondHalf = recent.slice(-half).reduce((a, s) => a + s.heapMB, 0) / half;

    if (avgNativePct > THRESHOLDS.nativePercent) {
      reasons.push(`原生 console 占主线程 ${avgNativePct.toFixed(1)}%（阈值 ${THRESHOLDS.nativePercent}%）`);
    }
    if (avgPerLine > THRESHOLDS.perLineUs) {
      reasons.push(`采集耗时 ${avgPerLine.toFixed(0)}µs/条（阈值 ${THRESHOLDS.perLineUs}）`);
    }
    if (avgPluginPct > 5) {
      reasons.push(`插件累计占主线程 ${avgPluginPct.toFixed(2)}%（阈值 5%）`);
    }
    // 落盘开销：这是实测中最贵的一环。每次 write() 的固定成本可达数百毫秒，
    // 成本主要在调用次数上，所以要同时看"耗时占比"和"单次耗时"。
    if (avgWritePct > THRESHOLDS.writePercent) {
      reasons.push(
        `落盘占每秒 ${avgWritePct.toFixed(0)}%（阈值 ${THRESHOLDS.writePercent}%），` +
          `每秒 ${avgWriteCalls.toFixed(1)} 次调用、单次 ${avgPerWriteMs.toFixed(0)}ms`
      );
    }
    if (maxDom > THRESHOLDS.domNodes) {
      reasons.push(`视图 DOM 节点数 ${maxDom}（阈值 ${THRESHOLDS.domNodes}）`);
    }
    if (maxBacklog > THRESHOLDS.backlog) {
      reasons.push(`页面侧缓冲积压 ${maxBacklog}（阈值 ${THRESHOLDS.backlog}）`);
    }
    if (secondHalf - firstHalf > 50) {
      reasons.push(`JS 堆持续增长 ${firstHalf.toFixed(0)}MB → ${secondHalf.toFixed(0)}MB`);
    }

    let level = 'ok';
    if (reasons.length > 0) level = 'warn';
    // 严重：原生 console 很贵、插件占主线程很多、积压严重，或落盘吃掉了大半秒
    if (avgNativePct > 40 || avgPluginPct > 15 || maxBacklog > 3000 || avgWritePct > 50) level = 'severe';

    return {
      level,
      reasons,
      stats: {
        avgPerLine,
        avgNativePct,
        avgPluginPct,
        avgWriteMs,
        avgWritePct,
        avgPerWriteMs,
        avgWriteCalls,
        maxDom,
        maxBacklog,
        avgRate,
      },
    };
  }
}

/**
 * 诊断记录器：把样本以 JSONL 追加写入文件，并提供人类可读的汇总。
 * 用传入的 FileSystemDirectoryHandle 写文件。
 */
class DiagWriter {
  constructor(dirHandle) {
    this.dirHandle = dirHandle;
    this.fileHandle = null;
    this.writable = null;
    this.pending = [];
    this.flushing = false;
    this.fileName = `${DIAG_FILE_PREFIX}-${new Date().toISOString().replace(/[:.]/g, '-')}.jsonl`;
  }

  async open() {
    this.fileHandle = await this.dirHandle.getFileHandle(this.fileName, { create: true });
    this.writable = await this.fileHandle.createWritable({ keepExistingData: false });
    return this.fileName;
  }

  /** 非阻塞入队：诊断写入绝不能拖慢主流程 */
  enqueue(obj) {
    this.pending.push(JSON.stringify(obj));
    this.scheduleFlush();
  }

  scheduleFlush() {
    if (this.flushing) return;
    this.flushing = true;
    setTimeout(() => this.flushNow(), 1000);
  }

  async flushNow() {
    this.flushing = false;
    if (!this.writable || this.pending.length === 0) return;
    const chunk = this.pending.join('\n') + '\n';
    this.pending = [];
    try {
      await this.writable.write(chunk);
    } catch (e) {
      // 诊断写入失败不能影响采集，静默丢弃
    }
  }

  /** 写一条人类可读的结论行 */
  note(text) {
    this.enqueue({ kind: 'note', at: new Date().toISOString(), text });
  }

  sample(s) {
    this.enqueue({ kind: 'sample', ...s });
  }

  async close() {
    await this.flushNow();
    if (this.writable) {
      try {
        await this.writable.close();
      } catch (e) {}
      this.writable = null;
    }
  }
}

// 普通 <script> 加载（非 module），挂到全局供 sidepanel.js 使用
window.CCDiag = { MetricsCollector, DiagWriter, THRESHOLDS };
