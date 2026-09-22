const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const syncBtn = document.getElementById('syncBtn');
const clearBtn = document.getElementById('clearBtn');
const changeDirBtn = document.getElementById('changeDirBtn');
const dirLabelEl = document.getElementById('dirLabel');
const statusEl = document.getElementById('status');
const tabInfoEl = document.getElementById('tabInfo');
const logViewEl = document.getElementById('logView');
const jumpBottomBtn = document.getElementById('jumpBottomBtn');
const lineCountEl = document.getElementById('lineCount');
const filterSummaryEl = document.getElementById('filterSummary');
const filterAllEl = document.getElementById('filterAll');
const levelCheckboxes = Array.from(document.querySelectorAll('.filter-menu input[data-level]'));
const searchInput = document.getElementById('searchInput');
const mergeToggle = document.getElementById('mergeToggle');
const statChips = {
  errors: document.getElementById('chipErrors'),
  warnings: document.getElementById('chipWarnings'),
  info: document.getElementById('chipInfo'),
  verbose: document.getElementById('chipVerbose'),
};
const prevErrorBtn = document.getElementById('prevErrorBtn');
const nextErrorBtn = document.getElementById('nextErrorBtn');
const diagBtn = document.getElementById('diagBtn');
const overheadEl = document.getElementById('overhead');
const muteNativeToggle = document.getElementById('muteNativeToggle');
const liveErrorsToggle = document.getElementById('liveErrorsToggle');
const fileOnlyToggle = document.getElementById('fileOnlyToggle');
const diagInfoEl = document.getElementById('diagInfo');

// ---------- 自动诊断 ----------
// 插件自己采集指标、自己判断异常、自己降级，不依赖人工观察。
const collector = new window.CCDiag.MetricsCollector();
let diagWriter = null;
let diagTimer = null;
let diagFileNote = '';
// 自动降级动作只做一次，避免反复切来切去
function persistToggles() {
  try {
    localStorage.setItem(
      'cc-toggles',
      JSON.stringify({
        muteNative: muteNativeToggle.checked,
        liveErrorsOnly: liveErrorsToggle.checked,
        fileOnly: fileOnlyToggle.checked,
      })
    );
  } catch (e) {}
}

function restoreToggles() {
  try {
    const raw = localStorage.getItem('cc-toggles');
    if (!raw) return;
    const t = JSON.parse(raw);
    if (typeof t.muteNative === 'boolean') muteNativeToggle.checked = t.muteNative;
    if (typeof t.liveErrorsOnly === 'boolean') liveErrorsToggle.checked = t.liveErrorsOnly;
    if (typeof t.fileOnly === 'boolean') fileOnlyToggle.checked = t.fileOnly;
    // 旧版默认勾了「只落盘」，会把实时错误也关掉。迁到「实时仅错误」。
    if (t.liveErrorsOnly === undefined && t.fileOnly === true) {
      liveErrorsToggle.checked = true;
      fileOnlyToggle.checked = false;
    }
  } catch (e) {}
}

restoreToggles();

const autoActions = { mutedNative: false, forcedFileOnly: false, clampedView: false, slowedWrites: false };

function diagLog(text) {
  if (diagWriter) diagWriter.note(text);
}

// 自动降级：这是"不需要人工判断"的关键——发现问题就直接动手
async function autoRemediate(result) {
  const changed = [];

  // 对策 0：落盘太贵 → 把写入频率降到最低
  // 实测单次 write() 固定开销可达数百毫秒，所以唯一有效的办法是减少调用次数。
  if (!autoActions.slowedWrites && result.stats.avgWritePct > 15) {
    autoActions.slowedWrites = true;
    writeBatchBytes = 4 * 1024 * 1024;
    writeFlushMs = 10000;
    changed.push('已自动降低落盘频率（单次写入开销过高）');
  }

  // 对策 1：原生 console 太贵，或拖角色时日志暴涨
  if (!autoActions.mutedNative && (result.stats.avgNativePct > 40 || result.stats.avgRate > 80)) {
    autoActions.mutedNative = true;
    muteNativeToggle.checked = true;
    persistToggles();
    await pushConfigToPage();
    changed.push('已自动开启静音（原生 console 开销过高或日志暴涨）');
  }

  // 对策 2：日志暴涨时 Info 刷屏会卡，但错误仍要实时看见
  if (!autoActions.forcedFileOnly && (result.stats.avgPluginPct > 15 || result.stats.avgRate > 80)) {
    autoActions.forcedFileOnly = true;
    liveErrorsToggle.checked = true;
    fileOnlyToggle.checked = false;
    persistToggles();
    await pushConfigToPage();
    changed.push('已自动改为实时仅错误/警告（Info 洪峰不刷侧边栏，错误仍立刻显示）');
  }

  // 对策 3：视图出问题 → 自动收紧视图上限并重建
  if (!autoActions.clampedView && result.stats.maxDom > 4000) {
    autoActions.clampedView = true;
    rebuildView();
    changed.push('已自动重建视图（DOM 节点数异常）');
  }

  if (changed.length > 0) {
    setStatus('自动降级：' + changed.join('；'), 'running');
    diagLog('自动降级：' + changed.join('；'));
    updateDiagDisplay(result, changed);
  }
}

function updateDiagDisplay(result, changed) {
  const s = result.stats;
  const level = result.level === 'severe' ? '严重' : result.level === 'warn' ? '注意' : '正常';
  let text =
    `自检[${level}] 速率 ${s.avgRate.toFixed(0)}/s｜采集 ${s.avgPerLine.toFixed(1)}µs/条｜` +
    `插件占 ${s.avgPluginPct.toFixed(2)}%｜原生 ${s.avgNativePct.toFixed(1)}%｜` +
    `落盘 ${s.avgWritePct.toFixed(0)}%(${s.avgWriteCalls.toFixed(1)}次/秒,单次${s.avgPerWriteMs.toFixed(0)}ms` +
    `,${writeWorker ? 'worker:' + writeWorkerMode : writable ? '主线程' : '未开'})｜` +
    `DOM ${s.maxDom}｜积压 ${s.maxBacklog}`;
  if (result.reasons.length) text += '\n⚠ ' + result.reasons.join('；');
  if (changed && changed.length) text += '\n✓ ' + changed.join('；');
  if (lastLongTaskNote) text += '\n⚠ ' + lastLongTaskNote;
  if (lastSelfCheck && !lastSelfCheck.stillOurs && lastSelfCheck.running) {
    text += '\n⚠ console.log 已被页面替换，采集可能拦不到日志（静音会失效）';
  }
  if (diagFileNote) text += '\n诊断文件：' + diagFileNote;
  diagInfoEl.textContent = text;
}

// 心跳：每秒采样一次、每 5 秒结算一次并做诊断。
// 整个过程自动完成，不需要人工观察。
function startDiagHeartbeat() {
  stopDiagHeartbeat();
  diagTimer = setInterval(async () => {
    // 采集环境指标
    if (performance.memory) {
      collector.setHeap(performance.memory.usedJSHeapSize / 1048576);
    }
    collector.setVisible(document.visibilityState === 'visible');

    // 向 worker 要一次落盘统计（它会在下一条消息里回报）
    if (writeWorker) writeWorker.postMessage({ type: 'stats' });

    diagSampleCount++;

    // 每 5 秒做一次页面侧自检：主要看 console 是否被叠包了多层。
    // 游戏热重载后如果叠包了，日志会被重复序列化多次，这是隐藏的性能杀手。
    if (diagSampleCount % 5 === 0) {
      await runSelfCheck();
    } else {
      return;
    }

    const sample = collector.flush();
    // 把写入方式记进每个样本，事后一眼就能判断 worker 是否真的生效
    sample.writeMode = writeWorker ? 'worker:' + writeWorkerMode : writable ? 'main' : 'none';
    if (diagWriter) diagWriter.sample(sample);

    const result = collector.diagnose(5);
    updateDiagDisplay(result);
    if (result.level !== 'ok') {
      if (diagWriter) diagWriter.note(`异常：${result.reasons.join('；')}`);
      autoRemediate(result);
    }
  }, 1000);
}

// 读取页面侧自检信息并记录；发现异常自动处理
let lastWrapperDepth = 0;
let lastSelfCheck = null;
let lastLongTaskNote = '';

async function runSelfCheck() {
  if (targetTabId == null) return;
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: targetTabId },
      func: () => (typeof window.__ccSelfCheck === 'function' ? window.__ccSelfCheck() : null),
    });
    const info = res && res.result;
    if (!info) return;

    lastWrapperDepth = info.wrapperDepth;
    lastSelfCheck = info;
    if (diagWriter) {
      diagWriter.enqueue({ kind: 'selfcheck', at: new Date().toISOString(), ...info });
    }

    // 长任务：页面主线程被阻塞超过 50ms 的任务（含 V8 垃圾回收）。
    // 这是"我的计时器测不到、但页面的确在卡"的主要来源。
    const lt = info.longTask;
    if (lt && lt.count > 0) {
      const msg = `页面主线程被阻塞 ${lt.count} 次，累计 ${lt.totalMs}ms，最长 ${lt.maxMs}ms（采集期间）`;
      diagLog(msg);
      // 长任务很严重的记录到自检摘要里
      if (lt.totalMs > 1000) {
        lastLongTaskNote = msg;
      }
    }

    // 被顶替检测：如果 console.log 已经不是我的包装层，说明引擎或其他脚本
    // 在页面初始化时（我注入之后）把它换掉了。这时"静音"是无效的，
    // 因为日志根本不再经过我的代码。
    if (!info.stillOurs && info.running) {
      const msg = '警告：console.log 已被页面/引擎替换，当前采集可能拦不到日志（静音也会失效）';
      diagLog(msg);
      setStatus(msg, 'error');
    }

    // 叠包检测：正常应该是 1 层。超过说明注入逻辑被破坏了。
    if (info.wrapperDepth > 1) {
      const msg = `检测到 console 被叠包 ${info.wrapperDepth} 层（应为 1 层），日志会被重复处理，正在修复`;
      setStatus(msg, 'error');
      diagLog(msg);
      await repairWrapper();
    }
  } catch (e) {
    // 标签页可能已关闭或还在加载，忽略
  }
}

// 修复叠包：把包装层全部拆掉，重新注入一次干净的
async function repairWrapper() {
  try {
    await chrome.scripting.executeScript({
      target: { tabId: targetTabId },
      world: 'MAIN',
      func: () => {
        // 沿 __ccOrig 链回溯到真正的原生函数，然后恢复
        const methods = ['log', 'warn', 'error', 'info', 'debug'];
        for (const m of methods) {
          let fn = console[m];
          let guard = 0;
          while (typeof fn === 'function' && fn.__ccWrapped && fn.__ccOrig && guard < 50) {
            fn = fn.__ccOrig;
            guard++;
          }
          if (typeof fn === 'function') console[m] = fn;
        }
        // 清掉安装标记，让下次注入重新装一层干净的
        window.__ccInstalled = false;
        window.__ccRunning = false;
      },
    });
    // 重新注入
    await chrome.scripting.executeScript({ target: { tabId: targetTabId }, world: 'MAIN', func: mainWorldCapture });
    await pushConfigToPage();
    diagLog('已修复 console 叠包，重新注入完成');
    setStatus('已修复 console 叠包（重新注入完成）', 'running');
  } catch (e) {
    diagLog('修复叠包失败：' + e.message);
  }
}


function stopDiagHeartbeat() {
  if (diagTimer) {
    clearInterval(diagTimer);
    diagTimer = null;
  }
}

let diagSampleCount = 0;

// 视图里保留的最大行数。这个数字直接决定了侧边栏占多少内存和 DOM 节点数，
// 不要调太大：每多一行就多一个常驻 DOM 节点，浏览器长时间运行会明显变卡。
const MAX_DISPLAY_ROWS = 2000;
// 错误汇总文件的行数上限，防止游戏刷错误时数组无限膨胀吃光内存
const MAX_ERROR_SUMMARY = 5000;
// 单条日志文本的长度上限
const MAX_TEXT_LENGTH = 2000;
// 页面侧缓冲区的上限：超过就丢最旧的，宁可丢日志也不能让浏览器卡死
const MAX_PAGE_BUFFER = 2000;
// 每批最多处理多少条（防御异常情况下的超大 batch）
const MAX_BATCH_SIZE = 3000;
// 渲染节流间隔
const RENDER_THROTTLE_MS = 250;
const SCROLL_BOTTOM_THRESHOLD = 30;

// 把捕获到的原始 level 归到 DevTools 风格的四个分类
const LEVEL_TO_CATEGORY = {
  debug: 'verbose',
  log: 'info',
  info: 'info',
  warn: 'warnings',
  error: 'errors',
  'uncaught-exception': 'errors',
  'unhandled-rejection': 'errors',
};

let targetTabId = null;
let dirHandle = null;
let writable = null;
// 写文件工作线程：所有磁盘 I/O 都在它那边做，主线程不碰
let writeWorker = null;
let writeWorkerMode = 'none'; // 'opfs' | 'stream' | 'none'
let totalCount = 0;
let droppedCount = 0;
let running = false;
let autoScroll = true;
let currentSessionTs = null;
let errorSummaryTruncated = false;

// 累计计数从「开始」起算，不受视图上限裁剪影响
let categoryCounts = { verbose: 0, info: 0, warnings: 0, errors: 0 };
// 错误汇总内容（有上限，供停止时写汇总文件用）
let errorSummaryLines = [];
// 速率统计：每秒更新一次
let rateWindowCount = 0;
let rateTimer = null;
let currentRate = 0;
// 当前视图里的行：只保留最近 MAX_DISPLAY_ROWS 条
let displayRows = [];
// 每一行都带一个只增不减的 seq。视图的尾部锚点用它标记，
// 因为 displayRows 会被裁剪，用行数推算位置在裁剪后会算错。
let nextSeq = 0;
// 视图里最后渲染的那一行的 seq。null 表示视图需要整体重建。
let appendCursor = null;
// 渲染节流
let renderTimer = null;
let pendingRebuild = false;

// ---------- IndexedDB：记住上次选择的保存目录句柄 ----------
const DB_NAME = 'console-capture-db';
const STORE_NAME = 'handles';
const HANDLE_KEY = 'targetDir';

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore(STORE_NAME);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function saveDirHandle(handle) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    tx.objectStore(STORE_NAME).put(handle, HANDLE_KEY);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function loadDirHandle() {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const req = tx.objectStore(STORE_NAME).get(HANDLE_KEY);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error);
  });
}

function updateDirLabel() {
  dirLabelEl.textContent = dirHandle ? `保存目录：${dirHandle.name}` : '保存目录：未设置';
}

// ---------- 通用工具 ----------
function activeCategories() {
  return levelCheckboxes.filter((cb) => cb.checked).map((cb) => cb.dataset.level);
}

function updateFilterSummary() {
  const active = activeCategories();
  if (active.length === levelCheckboxes.length) {
    filterSummaryEl.textContent = '全部级别';
  } else if (active.length === 0) {
    filterSummaryEl.textContent = '已全部隐藏';
  } else {
    filterSummaryEl.textContent = active.join(' / ');
  }
}

function updateStatsBar() {
  statChips.errors.textContent = `Errors: ${categoryCounts.errors}`;
  statChips.warnings.textContent = `Warnings: ${categoryCounts.warnings}`;
  statChips.info.textContent = `Info: ${categoryCounts.info}`;
  statChips.verbose.textContent = `Verbose: ${categoryCounts.verbose}`;
  prevErrorBtn.disabled = categoryCounts.errors === 0;
  nextErrorBtn.disabled = categoryCounts.errors === 0;
}

function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = kind;
}

function renderTabInfo(tab) {
  tabInfoEl.textContent = tab ? `目标标签页：${tab.title}\n${tab.url}` : '尚未选择标签页';
}

function formatLine(row) {
  const time = new Date(row.time).toISOString();
  return `[${time}] [${row.level}] ${row.text}`;
}

function rowText(row) {
  return formatLine(row) + (row.count > 1 ? ` (×${row.count})` : '');
}

function updateLineCount() {
  const parts = [`共 ${totalCount} 条`];
  if (running) parts.push(`速率 ${currentRate} 条/秒`);
  if (droppedCount > 0) parts.push(`因上限丢弃 ${droppedCount} 条`);
  parts.push(`视图保留 ${displayRows.length} 行`);
  if (mergeToggle.checked) parts.push('已合并重复');
  lineCountEl.textContent = parts.join('，');
}

// 每秒算一次日志速率，用来判断是不是真有高频刷屏
function startRateTimer() {
  stopRateTimer();
  rateWindowCount = 0;
  currentRate = 0;
  rateTimer = setInterval(() => {
    currentRate = rateWindowCount;
    rateWindowCount = 0;
    updateLineCount();
    updateOverheadDisplay();
  }, 1000);
}

function stopRateTimer() {
  if (rateTimer) {
    clearInterval(rateTimer);
    rateTimer = null;
  }
}

function scrollToBottomIfNeeded() {
  if (autoScroll) {
    logViewEl.scrollTop = logViewEl.scrollHeight;
    jumpBottomBtn.style.display = 'none';
  }
}

function createRowEl(row) {
  const div = document.createElement('div');
  div.className = 'log-line log-' + row.category;
  div.textContent = rowText(row);
  return div;
}

// 按当前筛选条件重建整个视图（只在筛选/搜索/合并变化时调用）
function rebuildView() {
  const activeCats = new Set(activeCategories());
  const term = searchInput.value.trim().toLowerCase();

  // 先按筛选条件过一遍，再按需要合并相邻重复
  const filtered = displayRows.filter((r) => {
    if (!activeCats.has(r.category)) return false;
    if (term && !r.text.toLowerCase().includes(term)) return false;
    return true;
  });

  let rows;
  if (mergeToggle.checked) {
    rows = [];
    for (const r of filtered) {
      const last = rows[rows.length - 1];
      if (last && last.category === r.category && last.text === r.text && last.level === r.level) {
        last.count += r.count;
        last.time = r.time;
      } else {
        rows.push({
          level: r.level,
          category: r.category,
          text: r.text,
          time: r.time,
          count: r.count,
          seq: r.seq,
        });
      }
    }
  } else {
    rows = filtered.map((r) => ({ ...r, count: 1 }));
    // 关掉合并时，如果原始行本身就是合并过的统计行，用计数拆开展示会更准；
    // 但为控制 DOM 数量，这里直接按 1 行近似展示，不做拆分。
  }

  const frag = document.createDocumentFragment();
  for (const row of rows) {
    frag.appendChild(createRowEl(row));
  }
  logViewEl.replaceChildren(frag);
  // 重建后，视图的尾部锚点 = 这次渲染的最后一行
  appendCursor = rows.length ? rows[rows.length - 1].seq : null;
  scrollToBottomIfNeeded();
  updateLineCount();
  updateFilterSummary();
}

function scheduleRender(rebuild) {
  if (rebuild) pendingRebuild = true;
  if (renderTimer) return;
  renderTimer = setTimeout(() => {
    renderTimer = null;
    const doRebuild = pendingRebuild;
    pendingRebuild = false;

    // 计时：诊断用，看渲染是否吃掉大量主线程
    const t0 = performance.now();
    try {
      if (doRebuild) {
        rebuildView();
        return;
      }
      // 兜底：节点数意外超过上限太多（正常不该发生），直接重建一次拉回边界，
      // 避免节点无限累积到某次集中销毁时把页面拖死。
      if (logViewEl.childElementCount > MAX_DISPLAY_ROWS * 2) {
        diagLog(`渲染兜底触发：DOM 节点 ${logViewEl.childElementCount} 超过上限两倍，强制重建`);
        rebuildView();
        return;
      }
      appendNewRows();
    } finally {
      collector.addRender(performance.now() - t0, logViewEl.childElementCount);
    }
  }, RENDER_THROTTLE_MS);
}

// 增量追加：只有"无筛选、无搜索、不合并"这种最常见的情况才走这条路
function canAppendIncrementally() {
  return (
    !mergeToggle.checked && !searchInput.value.trim() && activeCategories().length === levelCheckboxes.length
  );
}

function appendNewRows() {
  if (!canAppendIncrementally()) {
    rebuildView();
    return;
  }

  // 视图还是空的，或者没有可用的锚点，就整体重建一次
  if (appendCursor === null || displayRows.length === 0) {
    rebuildView();
    return;
  }

  // 从尾部往前找锚点：正常情况下它就是倒数第一个或倒数第二个，很快命中。
  // 不做"假设它在末尾"的捷径，那在数组被裁剪后会算错位置导致重复/漏渲染。
  let anchorIdx = -1;
  for (let i = displayRows.length - 1; i >= 0; i--) {
    if (displayRows[i].seq === appendCursor) {
      anchorIdx = i;
      break;
    }
  }
  if (anchorIdx === -1) {
    // 锚点已被裁掉，重建一次
    rebuildView();
    return;
  }

  const rowsToAppend = displayRows.slice(anchorIdx + 1);
  if (rowsToAppend.length === 0) {
    updateLineCount();
    return;
  }

  const frag = document.createDocumentFragment();
  for (const row of rowsToAppend) {
    frag.appendChild(createRowEl(row));
  }
  logViewEl.appendChild(frag);
  appendCursor = rowsToAppend[rowsToAppend.length - 1].seq;
  // DOM 节点数超过上限时，从头部删掉超出的部分。
  // 这一步必须真的执行，否则节点会无限累积，直到某次重建时集中销毁而卡死。
  let excess = logViewEl.childElementCount - MAX_DISPLAY_ROWS;
  while (excess > 0) {
    const el = logViewEl.firstChild;
    if (!el) break;
    logViewEl.removeChild(el);
    excess--;
  }

  scrollToBottomIfNeeded();
  updateLineCount();
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

// ---------- 注入到页面的采集脚本 ----------

// MAIN world：覆写 console 方法，缓冲后通过 postMessage 打包发出。
// 这个函数会被序列化后注入页面，不能引用外部作用域的任何变量。
function mainWorldCapture() {
  // 装上就不再卸：避免反复「开始」导致 console 被层层包装，
  // 每包一层，一条日志就要多做一次序列化和入队（曾导致主线程被打满）。
  if (window.__ccInstalled) {
    window.__ccRunning = true;
    return;
  }
  window.__ccInstalled = true;
  window.__ccRunning = true;

  const MAX_BUFFER = 2000; // 缓冲区上限，超出丢最旧的，防止后台节流时无限膨胀
  const MAX_BATCH = 500; // 单次搬运上限：即使缓冲里堆满了，也不一次搬运整批
  const MAX_TEXT = 2000; // 单条文本上限，防止一条巨型日志把内存顶爆
  const FLUSH_MS = 300;

  // 环形缓冲：不做 splice 搬移，写入位置循环前进。
  // 顺序在 flush 时还原，代价只有一次 slice。
  const ring = new Array(MAX_BUFFER);
  let ringLen = 0;
  let ringHead = 0;

  function ringPush(item) {
    ring[ringHead] = item;
    ringHead = (ringHead + 1) % MAX_BUFFER;
    if (ringLen < MAX_BUFFER) ringLen++;
  }

  // 取当前缓冲里最旧的 limit 条（保持从旧到新的顺序）
  function ringDrain(limit) {
    if (ringLen === 0) return null;
    const take = limit < ringLen ? limit : ringLen;
    const oldestIdx = (ringHead - ringLen + MAX_BUFFER) % MAX_BUFFER; // 最旧那条的下标
    const out = new Array(take);
    for (let i = 0; i < take; i++) out[i] = ring[(oldestIdx + i) % MAX_BUFFER];
    ringLen -= take;
    return out;
  }

  // 只描述参数的类型/形状，绝不读取对象的属性值。
  // 读取属性会触发 getter，而 Cocos 引擎对象的属性常带惰性计算——
  // 那等于逼着引擎在主线程上做本不该发生的运算（曾导致 Cocos 进程 CPU 飙高）。
  function describe(a) {
    try {
      if (a === null) return 'null';
      if (a === undefined) return 'undefined';

      const t = typeof a;
      if (t === 'string') return a.length > MAX_TEXT ? a.slice(0, MAX_TEXT) + '…' : a;
      if (t === 'number' || t === 'boolean' || t === 'bigint') return String(a);
      if (t === 'symbol') return a.toString();
      if (t === 'function') return '[function ' + (a.name || 'anonymous') + ']';

      if (Array.isArray(a)) return '[Array(' + a.length + ')]';
      if (a instanceof Date) {
        try {
          return a.toISOString();
        } catch (e) {
          return '[Date]';
        }
      }
      if (a instanceof Error) {
        // Error 的 message/stack 是自有数据属性，安全；仍然加保护
        try {
          return a.stack || a.message || '[Error]';
        } catch (e) {
          return '[Error]';
        }
      }

      if (t === 'object') {
        // 只取构造函数名，不枚举、不读取任何字段
        const name = (a.constructor && a.constructor.name) || 'Object';
        return '[' + name + ']';
      }

      return String(a);
    } catch (e) {
      return '[无法描述的参数]';
    }
  }

  function push(level, args) {
    if (!window.__ccRunning) return;
    let text;
    if (args.length === 1) {
      text = describe(args[0]);
    } else {
      const parts = new Array(args.length);
      for (let i = 0; i < args.length; i++) parts[i] = describe(args[i]);
      text = parts.join(' ');
    }
    if (text.length > MAX_TEXT) text = text.slice(0, MAX_TEXT) + '…[已截断]';
    ringPush({ level, text, time: Date.now() });
    stats.lines++;
  }

  // 采集配置：由侧边栏通过 postMessage 下发（避免每次改配置都重新注入脚本）
  const cfg = {
    // 静音：不调用原生 console。拖角色时这条是卡顿主因。
    muteNative: false,
    // 完全不刷侧边栏，只送纯文本写文件。
    fileOnly: false,
    // 侧边栏只收 error/warn；全文仍以纯文本落盘。Info 洪峰不克隆、不刷 DOM。
    liveErrorsOnly: false,
  };
  window.__ccCfg = cfg;

  // 真实开销统计：累计所有在页面侧花掉的时间，供侧边栏读取。
  // nativeMs 是原生 console 的耗时（没有 DevTools 时接近 0，开着则可能很贵）。
  const stats = { lines: 0, describeMs: 0, flushMs: 0, nativeMs: 0 };

  const EMPTY_ARGS = [];

  const methods = ['log', 'warn', 'error', 'info', 'debug'];
  methods.forEach((m) => {
    const orig = console[m];
    if (typeof orig !== 'function') return;
    // 通过标记保证即使外部脚本重复注入也只包装一次
    if (orig.__ccWrapped) return;
    // 用固定参数位而不是 rest（...args），避免每次调用都新建数组
    const wrapped = function (a0, a1, a2, a3, a4) {
      // 原生调用单独计时（这段不管有没有插件都会发生，不算我们的开销）
      if (orig && !cfg.muteNative) {
        const tNative = performance.now();
        try {
          orig.apply(console, arguments);
        } catch (e) {
          // 原生 console 出错不能影响页面
        }
        stats.nativeMs += performance.now() - tNative;
      }

      // 这一段才是我们的采集开销
      const t0 = performance.now();
      try {
        const n = arguments.length;
        if (n === 0) {
          push(m, EMPTY_ARGS);
        } else {
          const args = new Array(n);
          for (let i = 0; i < n; i++) args[i] = arguments[i];
          push(m, args);
        }
      } catch (e) {
        // 采集过程绝不能影响页面本身
      }
      stats.describeMs += performance.now() - t0;
    };
    wrapped.__ccWrapped = true;
    wrapped.__ccOrig = orig; // 保留原始引用，便于自检回溯包装层数
    console[m] = wrapped;
  });

  window.addEventListener('error', (e) => {
    try {
      if (!window.__ccRunning) return;
      let text = '';
      try {
        text = String((e.error && e.error.stack) || e.message || '');
      } catch (err) {
        text = '[错误对象无法读取]';
      }
      if (text.length > MAX_TEXT) text = text.slice(0, MAX_TEXT) + '…[已截断]';
      ringPush({ level: 'uncaught-exception', text, time: Date.now() });
    } catch (err) {}
  });

  window.addEventListener('unhandledrejection', (e) => {
    try {
      if (!window.__ccRunning) return;
      let text;
      try {
        text = String(e.reason);
      } catch (err) {
        text = '[无法转换的 rejection 原因]';
      }
      if (text.length > MAX_TEXT) text = text.slice(0, MAX_TEXT) + '…[已截断]';
      ringPush({ level: 'unhandled-rejection', text, time: Date.now() });
    } catch (err) {}
  });

  function isHotLevel(level) {
    return level === 'error' || level === 'warn' || level === 'uncaught-exception' || level === 'unhandled-rejection';
  }

  function flush() {
    if (!window.__ccRunning) return;
    const t0 = performance.now();
    for (let i = 0; i < 20; i++) {
      const batch = ringDrain(MAX_BATCH);
      if (!batch || batch.length === 0) break;

      try {
        // 全文始终以纯文本落盘（结构化克隆对象数组才贵）
        if (cfg.fileOnly || cfg.liveErrorsOnly) {
          window.postMessage({ __ccBatchText: formatBatchToText(batch) }, '*');
        }
        if (!cfg.fileOnly) {
          const live = cfg.liveErrorsOnly ? batch.filter((b) => isHotLevel(b.level)) : batch;
          if (live.length) window.postMessage({ __ccBatch: true, batch: live }, '*');
        }
      } catch (err) {
        break;
      }
      if (batch.length < MAX_BATCH) break;
    }
    stats.flushMs += performance.now() - t0;
  }

  // 自检：检查 console 是否被叠包了多层。
  // 游戏热重载/重新编译后，我的"只装一次"标记可能失效，
  // 导致同一条日志被序列化多次——这是最隐蔽的性能杀手。
  function countWrappers() {
    let depth = 0;
    let fn = console.log;
    const seen = new Set();
    while (typeof fn === 'function' && fn.__ccWrapped && !seen.has(fn)) {
      seen.add(fn);
      depth++;
      fn = fn.__ccOrig; // 需要包装时保存原始函数引用才能回溯
      if (!fn) break;
    }
    return depth;
  }

  // 采集长任务：主线程被阻塞超过 50ms 的任务。
  // 这是"我测不到但确实卡"的主要来源——比如 V8 垃圾回收、
  // 或页面自己做的重活。性能观察器能直接抓到，不依赖我的计时器。
  const longTasks = { count: 0, totalMs: 0, maxMs: 0 };
  try {
    if (typeof PerformanceObserver === 'function') {
      const po = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          longTasks.count++;
          longTasks.totalMs += e.duration;
          if (e.duration > longTasks.maxMs) longTasks.maxMs = e.duration;
        }
      });
      po.observe({ entryTypes: ['longtask'] });
    }
  } catch (e) {
    // 不支持 longtask 就跳过，不影响采集
  }

  // 暴露给侧边栏读取的自检信息
  window.__ccSelfCheck = function () {
    // 检查 console.log 是否还是我的包装层（可能被引擎顶替了）
    const stillOurs = !!(console.log && console.log.__ccWrapped);
    const lt = { count: longTasks.count, totalMs: Math.round(longTasks.totalMs), maxMs: Math.round(longTasks.maxMs) };
    longTasks.count = 0;
    longTasks.totalMs = 0;
    longTasks.maxMs = 0;
    return {
      installed: !!window.__ccInstalled,
      running: !!window.__ccRunning,
      wrapperDepth: countWrappers(),
      stillOurs,
      ringLen: ringLen,
      ringCap: MAX_BUFFER,
      bridgeInstalled: !!window.__ccBridgeInstalled,
      intervalCount: window.__ccIntervalCount || 0,
      longTask: lt,
    };
  };

  // 把一批日志拼成多行文本（只落盘模式用），避免传对象数组
  function formatBatchToText(batch) {
    const out = new Array(batch.length);
    for (let i = 0; i < batch.length; i++) {
      const b = batch[i];
      out[i] = '[' + new Date(b.time).toISOString() + '] [' + b.level + '] ' + b.text;
    }
    return out.join('\n') + '\n';
  }

  // 上报一次真实开销统计，并把窗口清零
  function reportStats() {
    const s = {
      lines: stats.lines,
      describeMs: stats.describeMs,
      flushMs: stats.flushMs,
      nativeMs: stats.nativeMs,
      backlog: ringLen, // 当前缓冲积压量，用于判断搬运是否跟不上
      wrapperDepth: countWrappers(),
    };
    stats.lines = 0;
    stats.describeMs = 0;
    stats.flushMs = 0;
    stats.nativeMs = 0;
    if (s.lines === 0 && s.flushMs === 0 && s.nativeMs === 0) return;
    try {
      window.postMessage({ __ccStats: s }, '*');
    } catch (e) {}
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const d = event.data;
    if (!d) return;
    if (d.__ccStatsRequest) {
      // 立刻上报一次，不等到下一个窗口
      reportStats();
      return;
    }
    // 侧边栏下发配置（静音模式等）
    if (d.__ccSetConfig) {
      if (typeof d.muteNative === 'boolean') cfg.muteNative = d.muteNative;
      if (typeof d.fileOnly === 'boolean') cfg.fileOnly = d.fileOnly;
      if (typeof d.liveErrorsOnly === 'boolean') cfg.liveErrorsOnly = d.liveErrorsOnly;
      reportStats();
    }
  });

  setInterval(() => {
    flush();
    reportStats();
  }, FLUSH_MS);
  window.__ccIntervalCount = (window.__ccIntervalCount || 0) + 1;

  // 页面切到后台时 setInterval 会被浏览器节流到一分钟一次，
  // 这里在切后台/切回来的时机补一次排空，避免缓冲区在后台堆积。
  document.addEventListener('visibilitychange', flush);
  window.addEventListener('pagehide', flush);
}

// ISOLATED world（默认）：把 MAIN world 的 postMessage 转发给扩展的 runtime 消息
function bridgeInject() {
  if (window.__ccBridgeInstalled) return;
  window.__ccBridgeInstalled = true;
  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data) return;

    if (data.__ccBatch) {
      try {
        const p = chrome.runtime.sendMessage({ type: 'cc-log-batch', batch: data.batch });
        // 侧边栏关闭或插件重载时这里会 reject，必须消化掉，
        // 否则每 300ms 产生一个未处理异常，反过来拖慢页面
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (e) {
        // 扩展上下文失效（Extension context invalidated），静默忽略
      }
      return;
    }

    // 只落盘模式：已经是拼好的文本，直接转发，不做结构化克隆
    if (typeof data.__ccBatchText === 'string') {
      try {
        const p = chrome.runtime.sendMessage({ type: 'cc-log-text', text: data.__ccBatchText });
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (e) {}
      return;
    }

    // 真实开销统计：转发给侧边栏
    if (data.__ccStats) {
      try {
        const p = chrome.runtime.sendMessage({ type: 'cc-stats', stats: data.__ccStats });
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (e) {}
    }
  });
}

function stopMainWorld() {
  window.__ccRunning = false;
}

// ---------- 批次落地：展示 + 落盘 ----------
async function appendBatch(batch) {
  if (!Array.isArray(batch) || batch.length === 0) return;

  // 防御异常情况下的大 batch
  let entries = batch;
  if (entries.length > MAX_BATCH_SIZE) {
    droppedCount += entries.length - MAX_BATCH_SIZE;
    entries = entries.slice(entries.length - MAX_BATCH_SIZE);
  }

  const fileLines = [];
  for (const entry of entries) {
    const category = LEVEL_TO_CATEGORY[entry.level] || 'info';
    // seq 只增不减，用来给增量渲染定位
    const row = { level: entry.level, category, text: entry.text, time: entry.time, count: 1, seq: nextSeq++ };

    categoryCounts[category]++;
    if (!liveErrorsToggle.checked) {
      totalCount++;
      rateWindowCount++;
    }

    // 追加到视图数组，超出上限时从头部丢弃（DOM 会在渲染时同步裁剪）
    displayRows.push(row);
    if (displayRows.length > MAX_DISPLAY_ROWS) {
      displayRows.splice(0, displayRows.length - MAX_DISPLAY_ROWS);
    }

    const line = formatLine(row);
    fileLines.push(line);
    if (category === 'errors') {
      errorSummaryLines.push(line);
      if (errorSummaryLines.length > MAX_ERROR_SUMMARY) {
        errorSummaryLines.shift();
        errorSummaryTruncated = true;
      }
    }
  }

  updateStatsBar();
  scheduleRender(false);

  // 实时仅错误时，全文已经由文本通道落盘，这里只刷新视图，避免写两遍
  if ((writeWorker || writable) && !liveErrorsToggle.checked && !fileOnlyToggle.checked) {
    enqueueWrite(fileLines.join('\n') + '\n');
  }
}

// ---------- 落盘：交给 worker 线程 ----------
// 实测 FileSystemWritableFileStream.write() 的耗时极不稳定：中位数只有 2ms，
// 但会偶发飙到 316ms / 722ms / 2442ms，且与数据量无关（2442ms 那次只写 3.3KB，
// 而 45KB 那次只花 4ms）。这是 Chrome 提交文件时被系统 I/O 阻塞，
// 只要发生在主线程上，面板和整个浏览器就跟着卡。
//
// 所以文件 I/O 全部搬进 write-worker.js：主线程只负责把文本丢过去，
// 尖峰再大也只卡 worker 自己那条线程，不影响渲染和游戏。
// 这里仍保留一层小的合并（攒 256KB 或 2 秒），只是为了减少 postMessage 次数，
// 不再需要靠它规避写入尖峰。
// 主线程这边只负责合并后交给 worker，所以可以攒得更久一些：
// 之前 2 秒的定时器导致每次只攒 13KB 就冲刷，白白多出很多次调用。
let writeBatchBytes = 256 * 1024;
let writeFlushMs = 8000;
const MAX_PENDING_BYTES = 8 * 1024 * 1024; // 内存里最多攒 8MB，防极端情况吃光内存
let pendingWriteChunks = [];
let pendingWriteBytes = 0;
let writeFlushTimer = null;

function enqueueWrite(text) {
  pendingWriteChunks.push(text);
  pendingWriteBytes += text.length;

  // 防内存失控：攒太多时立刻交出去
  if (pendingWriteBytes >= MAX_PENDING_BYTES || pendingWriteBytes >= writeBatchBytes) {
    flushWrite();
    return;
  }
  if (!writeFlushTimer) {
    writeFlushTimer = setTimeout(() => {
      writeFlushTimer = null;
      flushWrite();
    }, writeFlushMs);
  }
}

// 把攒着的文本交给 worker。这个函数不再做真正的 I/O，
// 所以是同步的、几乎零耗时——真正的写入耗时由 worker 统计后回报。
function flushWrite() {
  if (pendingWriteChunks.length === 0) return;
  const payload = pendingWriteChunks.join('');
  pendingWriteChunks = [];
  pendingWriteBytes = 0;

  if (writeWorker) {
    writeWorker.postMessage({ type: 'write', text: payload });
    return;
  }

  // worker 不可用时的退路：仍在主线程写，但会被诊断记录下来
  if (writable) {
    const tw = performance.now();
    writable
      .write(payload)
      .catch((e) => {
        setStatus('写入本地文件失败：' + e.message, 'error');
        diagLog('写入本地文件失败：' + e.message);
      })
      .finally(() => {
        const ms = performance.now() - tw;
        collector.addWrite(ms);
        collector.addWriteCall(ms, payload.length);
      });
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg) return;
  if (msg.type === 'cc-log-batch' && running) {
    appendBatch(msg.batch);
  } else if (msg.type === 'cc-log-text' && running) {
    appendTextBatch(msg.text);
  } else if (msg.type === 'cc-stats') {
    applyStats(msg.stats);
  }
});

// 把两个开关的当前状态下发给页面（改完立即生效，不需要重新注入）
async function pushConfigToPage() {
  if (targetTabId == null) return;
  try {
    const cfg = {
      muteNative: muteNativeToggle.checked,
      fileOnly: fileOnlyToggle.checked,
      liveErrorsOnly: liveErrorsToggle.checked,
    };
    await chrome.scripting.executeScript({
      target: { tabId: targetTabId },
      world: 'MAIN',
      func: (c) => window.postMessage({ __ccSetConfig: true, ...c }, '*'),
      args: [cfg],
    });
  } catch (e) {
    // 页面可能已关闭或还没注入，忽略
  }
}

muteNativeToggle.addEventListener('change', () => {
  persistToggles();
  pushConfigToPage();
});
liveErrorsToggle.addEventListener('change', () => {
  persistToggles();
  pushConfigToPage();
  if (liveErrorsToggle.checked && fileOnlyToggle.checked) {
    fileOnlyToggle.checked = false;
    persistToggles();
  }
  setStatus(
    liveErrorsToggle.checked
      ? '实时仅错误/警告：Info 不刷侧边栏，错误出现会立刻显示；完整日志仍写入文件'
      : '侧边栏显示全部级别（拖角色时可能卡）',
    'running'
  );
});
fileOnlyToggle.addEventListener('change', () => {
  persistToggles();
  if (fileOnlyToggle.checked) {
    setStatus('完全不刷侧边栏：日志只写入文件', 'running');
  }
  pushConfigToPage();
});

// 只落盘模式：文本直接写文件，不建行对象、不进视图，也不计分类
let textLinesWritten = 0;

async function appendTextBatch(text) {
  if (!text) return;
  const lines = text.split('\n');
  // 末尾会多一个空串，去掉
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  const n = lines.length;
  if (n === 0) return;

  textLinesWritten += n;
  totalCount += n;
  rateWindowCount += n;
  updateLineCount();

  if (writeWorker || writable) {
    enqueueWrite(text);
  }
}

// 累计页面侧真实开销，算出「每条日志在页面里花了多少微秒」
let statsAccum = { lines: 0, describeMs: 0, flushMs: 0, nativeMs: 0 };
let statsSince = Date.now();

function applyStats(s) {
  if (!s) return;
  statsAccum.lines += s.lines || 0;
  statsAccum.describeMs += s.describeMs || 0;
  statsAccum.flushMs += s.flushMs || 0;
  statsAccum.nativeMs += s.nativeMs || 0;
  collector.addPageStats(s);
  updateOverheadDisplay();
}

function updateOverheadDisplay() {
  if (!running) return;
  const elapsedSec = (Date.now() - statsSince) / 1000;
  if (elapsedSec < 1) return;
  const mineMs = statsAccum.describeMs + statsAccum.flushMs;
  const lines = statsAccum.lines;
  const perLineUs = lines > 0 ? (mineMs / lines) * 1000 : 0;
  const minePct = (mineMs / (elapsedSec * 1000)) * 100;
  const nativePct = (statsAccum.nativeMs / (elapsedSec * 1000)) * 100;
  overheadEl.textContent =
    `页面侧开销：插件 ${perLineUs.toFixed(2)} µs/条，占主线程 ${minePct.toFixed(2)}%；` +
    `原生 console 占 ${nativePct.toFixed(2)}%（${lines} 条 / ${elapsedSec.toFixed(0)}s）`;
}

function resetStats() {
  statsAccum = { lines: 0, describeMs: 0, flushMs: 0, nativeMs: 0 };
  statsSince = Date.now();
  overheadEl.textContent = '页面侧开销：统计中…';
}

// ---------- 开始 / 停止 ----------
async function ensureDirHandle() {
  if (dirHandle) {
    try {
      const perm = await dirHandle.queryPermission({ mode: 'readwrite' });
      if (perm === 'granted') return dirHandle;
      const req = await dirHandle.requestPermission({ mode: 'readwrite' });
      if (req === 'granted') return dirHandle;
    } catch (e) {
      // 句柄可能已失效，走下面重新选择
    }
  }
  const handle = await window.showDirectoryPicker();
  dirHandle = handle;
  await saveDirHandle(handle);
  updateDirLabel();
  return handle;
}

// 启动诊断写入器。诊断文件用 .jsonl（每行一个 JSON），便于事后分析。
async function startDiagWriter(handle) {
  try {
    if (diagWriter) {
      await diagWriter.close();
      diagWriter = null;
    }
    diagWriter = new window.CCDiag.DiagWriter(handle);
    const name = await diagWriter.open();
    diagFileNote = name;
    diagLog('采集开始');
    diagLog(`阈值：${JSON.stringify(window.CCDiag.THRESHOLDS)}`);
    diagLog(
      `配置：静音=${muteNativeToggle.checked} 实时仅错误=${liveErrorsToggle.checked} 不刷侧边栏=${fileOnlyToggle.checked} ` +
        `视图上限=${MAX_DISPLAY_ROWS} 页面缓冲上限=2000 单批=500`
    );
  } catch (e) {
    diagWriter = null;
    diagFileNote = '(诊断文件创建失败：' + e.message + ')';
  }
}

// ---------- 写文件工作线程的生命周期 ----------
// 启动 worker 并让它打开日志文件。成功返回 true。
function startWriteWorker(dirHandle, fileName) {
  return new Promise((resolve) => {
    let w = null;
    try {
      stopWriteWorker();
      // 必须用扩展的绝对 URL：侧边栏页面里相对路径不一定能解析到
      const url = chrome.runtime.getURL('write-worker.js');
      w = new Worker(url);
    } catch (e) {
      diagLog('创建 worker 失败：' + e.message);
      setStatus('写文件 worker 创建失败，改用主线程写入：' + e.message, 'error');
      resolve(false);
      return;
    }

    let settled = false;
    const fail = (why) => {
      if (settled) return;
      settled = true;
      diagLog('worker 启动失败：' + why);
      try {
        w.terminate();
      } catch (e2) {}
      resolve(false);
    };

    w.onmessage = (e) => {
      const msg = e.data;
      if (!msg) return;

      if (msg.type === 'opened') {
        writeWorker = w;
        writeWorkerMode = msg.mode;
        diagLog(`写文件 worker 已启动，模式=${msg.mode}${msg.mode === 'opfs' ? '（实时只追加写 OPFS，点同步或停止才拷到所选目录）' : ''}`);
        if (!settled) {
          settled = true;
          resolve(true);
        }
        return;
      }

      // 打开文件阶段就报错 -> 直接判定失败，退回主线程
      if (msg.type === 'error') {
        if (!settled) {
          fail(msg.message);
          return;
        }
        diagLog('worker 写入错误：' + msg.message);
        return;
      }

      // worker 回报的真实落盘开销：这才是准确数字，主线程自己测不到
      if (msg.type === 'stats') {
        const s = msg.stats;
        if (s && s.calls > 0) {
          collector.addWrite(s.totalMs);
          collector.addWriteCall(s.totalMs / s.calls, s.bytes);
          // calls 可能 >1，把剩余次数补上，保证 writeCalls 统计准确
          for (let i = 1; i < s.calls; i++) collector.addWriteCall(0, 0);
        }
        if (s && s.queued > 200) {
          diagLog(`写入队列积压 ${s.queued} 批，磁盘跟不上`);
        }
        return;
      }

      if (msg.type === 'exported') {
        const kb = msg.bytes > 0 ? (msg.bytes / 1024).toFixed(1) : '0';
        setStatus(`已同步到所选目录（${kb} KB）`, 'running');
        diagLog(`手动同步完成：${kb} KB`);
        return;
      }
    };

    w.onerror = (err) => {
      // Worker 里的语法错误/加载失败会走这里
      const why = err && err.message ? err.message : `加载失败（${err && err.filename ? err.filename : '未知文件'}）`;
      fail(why);
    };

    w.onmessageerror = () => fail('消息序列化失败（dirHandle 可能无法传递给 worker）');

    try {
      w.postMessage({ type: 'open', dirHandle, fileName });
    } catch (e) {
      fail('无法把目录句柄传给 worker：' + e.message);
      return;
    }

    // 超时保护：3 秒还没开起来就退回主线程
    setTimeout(() => fail('3 秒内没有响应'), 3000);
  });
}

function stopWriteWorker() {
  if (writeWorker) {
    try {
      writeWorker.terminate();
    } catch (e) {}
    writeWorker = null;
  }
  writeWorkerMode = 'none';
}

// 请 worker 把文件收尾关闭，等它确认后再继续
function closeWriteWorker() {
  return new Promise((resolve) => {
    if (!writeWorker) {
      resolve();
      return;
    }
    const w = writeWorker;
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      try {
        w.terminate();
      } catch (e) {}
      writeWorker = null;
      writeWorkerMode = 'none';
      resolve();
    };
    const prev = w.onmessage;
    w.onmessage = (e) => {
      if (e.data && e.data.type === 'closed') {
        finish();
        return;
      }
      if (prev) prev(e);
    };
    w.postMessage({ type: 'close' });
    setTimeout(finish, 30000); // 拷贝到用户目录可能稍慢
  });
}

startBtn.addEventListener('click', async () => {
  try {
    const tab = await getActiveTab();
    if (!tab) {
      setStatus('未找到可用的标签页', 'error');
      return;
    }
    targetTabId = tab.id;
    renderTabInfo(tab);

    const handle = await ensureDirHandle();

    currentSessionTs = new Date().toISOString().replace(/[:.]/g, '-');
    const logFileName = `console-log-${currentSessionTs}.txt`;

    // 优先让 worker 接管文件写入；worker 起不来才退回主线程写
    const workerOk = await startWriteWorker(handle, logFileName);
    if (!workerOk) {
      const fileHandle = await handle.getFileHandle(logFileName, { create: true });
      writable = await fileHandle.createWritable({ keepExistingData: false });
      diagLog('worker 不可用，退回主线程写入');
    }

    // 诊断文件也写到同一个目录（用户自己选的，通常不是 Cocos 项目内）
    await startDiagWriter(handle);

    await chrome.scripting.executeScript({ target: { tabId: targetTabId }, world: 'MAIN', func: mainWorldCapture });
    await chrome.scripting.executeScript({ target: { tabId: targetTabId }, func: bridgeInject });
    await pushConfigToPage();

    // 新一次采集：重置计数和视图，避免和上一次的数据混在一起
    displayRows = [];
    nextSeq = 0;
    appendCursor = null;
    errorSummaryLines = [];
    errorSummaryTruncated = false;
    categoryCounts = { verbose: 0, info: 0, warnings: 0, errors: 0 };
    totalCount = 0;
    droppedCount = 0;
    updateStatsBar();
    rebuildView();

    running = true;
    startBtn.disabled = true;
    stopBtn.disabled = false;
    syncBtn.disabled = writeWorkerMode !== 'opfs';
    changeDirBtn.disabled = true;
    startRateTimer();
    resetStats();
    startDiagHeartbeat();
    const modeNote = writeWorker ? `worker:${writeWorkerMode}` : '主线程';
    const extra = writeWorkerMode === 'opfs' ? '；实时只追加到浏览器缓存，点「同步到目录」或「停止」才写入所选文件夹' : '';
    setStatus(`采集中 → ${logFileName}（写入方式：${modeNote}${extra}）`, 'running');
  } catch (e) {
    setStatus('开始失败：' + e.message, 'error');
  }
});

async function stopCapture(reason) {
  running = false;
  stopRateTimer();
  stopDiagHeartbeat();
  if (targetTabId != null) {
    try {
      await chrome.scripting.executeScript({ target: { tabId: targetTabId }, world: 'MAIN', func: stopMainWorld });
    } catch (e) {
      // 标签页可能已关闭，忽略
    }
  }
  if (renderTimer) {
    clearTimeout(renderTimer);
    renderTimer = null;
  }

  // 关文件前必须先把攒着的日志全交出去，否则会丢最后一批
  if (writeFlushTimer) {
    clearTimeout(writeFlushTimer);
    writeFlushTimer = null;
  }
  flushWrite();

  // 让 worker 把队列写完并关闭文件
  await closeWriteWorker();

  // 收尾诊断文件：把最后一段窗口和结论也记下来
  if (diagWriter) {
    try {
      const finalSample = collector.flush();
      diagWriter.sample(finalSample);
      diagWriter.note('采集结束');
      await diagWriter.close();
    } catch (e) {
      // 忽略
    }
    diagWriter = null;
  }

  if (writable) {
    try {
      await writable.close();
    } catch (e) {
      // 忽略关闭失败
    }
    writable = null;
  }

  let errorFileNote = '';
  if (dirHandle && errorSummaryLines.length > 0 && currentSessionTs) {
    try {
      const errFileHandle = await dirHandle.getFileHandle(`console-log-${currentSessionTs}-errors.txt`, {
        create: true,
      });
      const errWritable = await errFileHandle.createWritable({ keepExistingData: false });
      await errWritable.write(errorSummaryLines.join('\n') + '\n');
      await errWritable.close();
      errorFileNote =
        `\n错误汇总：${errFileHandle.name}（本次记录 ${errorSummaryLines.length} 条` +
        `${errorSummaryTruncated ? '，已达上限，更早的错误未包含' : ''}）`;
    } catch (e) {
      errorFileNote = '\n错误汇总文件写入失败：' + e.message;
    }
  }

  startBtn.disabled = false;
  stopBtn.disabled = true;
  syncBtn.disabled = true;
  changeDirBtn.disabled = false;
  setStatus((reason || '已停止') + errorFileNote, 'stopped');
}

stopBtn.addEventListener('click', () => stopCapture('已停止'));

syncBtn.addEventListener('click', () => {
  if (!writeWorker || writeWorkerMode !== 'opfs') {
    setStatus('当前不是 OPFS 模式，日志已在直接写入所选目录', 'stopped');
    return;
  }
  flushWrite();
  writeWorker.postMessage({ type: 'export' });
  setStatus('正在同步到所选目录…', 'running');
});

// ---------- 自动检测页面被重新加载/重新编译 ----------
// 游戏热重载或 Cocos 重新编译会让页面重新加载，注入的脚本随之消失，
// 采集会静默停止、日志看起来"卡住"。这里自动发现并自动重新注入。
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
  if (tabId !== targetTabId || !running) return;
  if (changeInfo.status !== 'complete') return;
  const msg = '检测到目标页面重新加载（可能是游戏重新编译），正在自动重新注入采集脚本';
  setStatus(msg, 'running');
  diagLog(msg);
  try {
    await chrome.scripting.executeScript({ target: { tabId }, world: 'MAIN', func: mainWorldCapture });
    await chrome.scripting.executeScript({ target: { tabId }, func: bridgeInject });
    await pushConfigToPage();
    diagLog('重新注入完成，采集继续');
    setStatus('页面重载后已自动恢复采集', 'running');
  } catch (e) {
    diagLog('重新注入失败：' + e.message);
    setStatus('页面重载后重新注入失败：' + e.message, 'error');
  }
});

changeDirBtn.addEventListener('click', async () => {
  try {
    const handle = await window.showDirectoryPicker();
    dirHandle = handle;
    await saveDirHandle(handle);
    updateDirLabel();
  } catch (e) {
    // 用户取消了选择框，忽略
  }
});

clearBtn.addEventListener('click', () => {
  // 只清空视图，累计的统计数字和错误汇总保持不变
  displayRows = [];
  appendCursor = null;
  rebuildView();
});

// ---------- 实时开销显示：直接读页面侧统计，不用假样本 ----------
diagBtn.addEventListener('click', async () => {
  if (targetTabId == null) {
    setStatus('请先点「开始」选定一个目标标签页', 'error');
    return;
  }
  // 请求页面立刻上报一次这段时间的真实开销
  try {
    await chrome.scripting.executeScript({
      target: { tabId: targetTabId },
      func: () => window.postMessage({ __ccStatsRequest: true }, '*'),
    });
  } catch (e) {
    setStatus('读取开销失败：' + e.message, 'error');
    return;
  }
  updateOverheadDisplay();
  setStatus('已刷新开销统计（数据来自页面里真实发生的日志处理）', 'running');
});

// ---------- 级别筛选 / 搜索 / 合并 ----------
levelCheckboxes.forEach((cb) => {
  cb.addEventListener('change', () => {
    filterAllEl.checked = levelCheckboxes.every((c) => c.checked);
    scheduleRender(true);
  });
});

filterAllEl.addEventListener('change', () => {
  levelCheckboxes.forEach((cb) => {
    cb.checked = filterAllEl.checked;
  });
  scheduleRender(true);
});

searchInput.addEventListener('input', () => scheduleRender(true));
mergeToggle.addEventListener('change', () => scheduleRender(true));

// ---------- 统计条 / 错误跳转 ----------
function showOnlyCategory(category) {
  levelCheckboxes.forEach((cb) => {
    cb.checked = cb.dataset.level === category;
  });
  filterAllEl.checked = false;
  rebuildView();
}

// 在当前渲染出来的日志里，跳到上一条/下一条 Errors
function jumpToError(direction) {
  const errorEls = Array.from(logViewEl.querySelectorAll('.log-errors'));
  if (errorEls.length === 0) return;
  const currentScroll = logViewEl.scrollTop;
  let target;
  if (direction === 'next') {
    target = errorEls.find((el) => el.offsetTop > currentScroll + 5) || errorEls[errorEls.length - 1];
  } else {
    const before = errorEls.filter((el) => el.offsetTop < currentScroll - 5);
    target = before.length ? before[before.length - 1] : errorEls[0];
  }
  autoScroll = false;
  logViewEl.scrollTop = Math.max(0, target.offsetTop - 8);
  jumpBottomBtn.style.display =
    logViewEl.scrollHeight - logViewEl.scrollTop - logViewEl.clientHeight < SCROLL_BOTTOM_THRESHOLD ? 'none' : 'block';
}

Object.entries(statChips).forEach(([category, el]) => {
  el.addEventListener('click', () => showOnlyCategory(category));
});
prevErrorBtn.addEventListener('click', () => jumpToError('prev'));
nextErrorBtn.addEventListener('click', () => jumpToError('next'));

// ---------- 自动滚动 ----------
logViewEl.addEventListener('scroll', () => {
  const atBottom = logViewEl.scrollHeight - logViewEl.scrollTop - logViewEl.clientHeight < SCROLL_BOTTOM_THRESHOLD;
  autoScroll = atBottom;
  jumpBottomBtn.style.display = atBottom ? 'none' : 'block';
});

jumpBottomBtn.addEventListener('click', () => {
  autoScroll = true;
  logViewEl.scrollTop = logViewEl.scrollHeight;
  jumpBottomBtn.style.display = 'none';
});

// ---------- 标签页/生命周期 ----------
chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === targetTabId && running) {
    stopCapture('目标标签页已关闭');
  }
});

window.addEventListener('beforeunload', () => {
  // 侧边栏关闭时通知页面停止采集，避免页面继续把日志往一个没人接收的地方发
  if (running && targetTabId != null) {
    try {
      chrome.scripting.executeScript({ target: { tabId: targetTabId }, world: 'MAIN', func: stopMainWorld });
    } catch (e) {
      // 忽略
    }
  }
  if (writable) {
    writable.close().catch(() => {});
  }
});

getActiveTab().then(renderTabInfo);
chrome.tabs.onActivated.addListener(async () => {
  if (!running) {
    const tab = await getActiveTab();
    renderTabInfo(tab);
  }
});

loadDirHandle()
  .then((handle) => {
    if (handle) {
      dirHandle = handle;
      updateDirLabel();
    }
  })
  .catch(() => {});

updateStatsBar();
updateLineCount();
