const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
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
// 自测结果的落点：由 runtime.onMessage 收到后调用
let measureResolve = () => {};
// 当前视图里的行：只保留最近 MAX_DISPLAY_ROWS 条
let displayRows = [];
// 每一行都带一个只增不减的 seq。用它来标记「视图已经渲染到哪一行了」，
// 因为 displayRows 会被裁剪，用行数推算位置在裁剪后会算错（曾导致视图卡住不再更新）。
let nextSeq = 0;
// 视图里最后渲染的那一行。为 null 表示视图是空的 / 需要整体重建。
let lastRenderedSeq = null;
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
  // 记录视图渲染到了哪一行，后续增量追加以它为锚点
  lastRenderedSeq = rows.length ? rows[rows.length - 1].seq : null;
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
    if (doRebuild) {
      rebuildView();
    } else {
      appendNewRows();
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
  if (lastRenderedSeq === null || displayRows.length === 0) {
    rebuildView();
    return;
  }

  // 已经渲染到的那个 seq 可能因为视图裁剪被丢掉了，这种情况下也重建
  const anchorIdx = displayRows.findIndex((r) => r.seq === lastRenderedSeq);
  if (anchorIdx === -1) {
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
  lastRenderedSeq = rowsToAppend[rowsToAppend.length - 1].seq;

  // DOM 节点数超过上限时，从头部删掉超出的部分。
  // 不用去同步 lastRenderedSeq——它是尾部锚点，删头部不会影响它。
  let renderedNow = logViewEl.childElementCount;
  let excess = renderedNow - MAX_DISPLAY_ROWS;
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
  }

  const EMPTY_ARGS = [];

  const methods = ['log', 'warn', 'error', 'info', 'debug'];
  methods.forEach((m) => {
    const orig = console[m];
    if (typeof orig !== 'function') return;
    // 通过标记保证即使外部脚本重复注入也只包装一次
    if (orig.__ccWrapped) return;
    // 用固定参数位而不是 rest（...args），避免每次调用都新建数组
    const wrapped = function (a0, a1, a2, a3, a4) {
      if (orig) orig.apply(console, arguments);
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
    };
    wrapped.__ccWrapped = true;
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

  function flush() {
    if (!window.__ccRunning) return;
    // 循环排空：每次最多搬 MAX_BATCH 条，避免单个巨型批次，
    // 但也不让日志在缓冲里积压（有上限保护，最多转几次就空了）。
    for (let i = 0; i < 20; i++) {
      const batch = ringDrain(MAX_BATCH);
      if (!batch || batch.length === 0) return;
      try {
        window.postMessage({ __ccBatch: true, batch }, '*');
      } catch (err) {
        // postMessage 失败（理论上不会）时直接丢弃这一批，不重试堆积
        return;
      }
      if (batch.length < MAX_BATCH) return;
    }
  }

  // 采集开销自测：量一段真实的单条日志成本（describe + 环形缓冲写入），
  // 结果发回侧边栏显示，用来判断是不是这段代码在吃页面 CPU。
  function measureCost() {
    const N = 20000;
    const sample = [
      'MainName关闭界面 UILogin goTo checkTaskJumpRes onTick render nextFrame update',
      12345,
      { a: 1, b: 2, c: 3 },
      [1, 2, 3],
    ];
    const t0 = performance.now();
    for (let i = 0; i < N; i++) {
      const parts = new Array(sample.length);
      for (let j = 0; j < sample.length; j++) parts[j] = describe(sample[j]);
      ringPush({ level: 'log', text: parts.join(' '), time: 0 });
    }
    const elapsed = performance.now() - t0;
    // 测完把这几万条塞进去的内容清掉，避免污染真实日志
    ringLen = 0;
    ringHead = 0;
    return (elapsed / N) * 1000; // 返回 µs/条
  }

  // 侧边栏请求自测时回一个结果
  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    if (event.data && event.data.__ccMeasureRequest) {
      let us = -1;
      try {
        us = measureCost();
      } catch (e) {
        us = -1;
      }
      try {
        window.postMessage({ __ccMeasureResult: us }, '*');
      } catch (e) {}
    }
  });

  setInterval(flush, FLUSH_MS);

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

    // 自测结果：转发给侧边栏（侧边栏和页面不是同一个 window，收不到页面自己发的消息）
    if (typeof data.__ccMeasureResult === 'number') {
      try {
        const p = chrome.runtime.sendMessage({ type: 'cc-measure-result', us: data.__ccMeasureResult });
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
    totalCount++;
    rateWindowCount++;

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
  // 视图按节流渲染，不做全量重建
  scheduleRender(false);

  if (writable) {
    try {
      await writable.write(fileLines.join('\n') + '\n');
    } catch (e) {
      setStatus('写入本地文件失败：' + e.message, 'error');
    }
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg) return;
  if (msg.type === 'cc-log-batch' && running) {
    appendBatch(msg.batch);
  } else if (msg.type === 'cc-measure-result') {
    measureResolve(msg.us);
  }
});

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
    const fileHandle = await handle.getFileHandle(`console-log-${currentSessionTs}.txt`, { create: true });
    writable = await fileHandle.createWritable({ keepExistingData: false });

    await chrome.scripting.executeScript({ target: { tabId: targetTabId }, world: 'MAIN', func: mainWorldCapture });
    await chrome.scripting.executeScript({ target: { tabId: targetTabId }, func: bridgeInject });

    // 新一次采集：重置计数和视图，避免和上一次的数据混在一起
    displayRows = [];
    nextSeq = 0;
    lastRenderedSeq = null;
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
    changeDirBtn.disabled = true;
    startRateTimer();
    setStatus(`采集中 → ${fileHandle.name}`, 'running');
  } catch (e) {
    setStatus('开始失败：' + e.message, 'error');
  }
});

async function stopCapture(reason) {
  running = false;
  stopRateTimer();
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
  changeDirBtn.disabled = false;
  setStatus((reason || '已停止') + errorFileNote, 'stopped');
}

stopBtn.addEventListener('click', () => stopCapture('已停止'));

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
  lastRenderedSeq = null;
  rebuildView();
});

// ---------- 自测：量页面侧每条日志的真实开销 ----------
diagBtn.addEventListener('click', async () => {
  if (targetTabId == null) {
    setStatus('请先点「开始」选定一个目标标签页，再自测', 'error');
    return;
  }
  setStatus('正在自测…（会在目标页面里跑 2 万次采集逻辑，约一两秒）', 'running');

  const result = await new Promise((resolve) => {
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      resolve(v);
    };
    measureResolve = (us) => finish(us);

    chrome.scripting
      .executeScript({
        target: { tabId: targetTabId },
        func: () => window.postMessage({ __ccMeasureRequest: true }, '*'),
      })
      .catch(() => finish(-1));

    setTimeout(() => finish(-1), 8000);
  });

  measureResolve = () => {};

  if (result === null || result < 0) {
    setStatus('自测没有拿到结果（页面里可能还没注入采集脚本，请先点「开始」并刷新页面）', 'error');
    return;
  }

  const perLineUs = result;
  const at100 = (perLineUs * 100) / 1000; // 100 条/秒时，每秒占用主线程的毫秒数
  setStatus(
    `自测结果：每条日志在页面里约 ${perLineUs.toFixed(2)} µs\n` +
      `按 100 条/秒 估算：约 ${at100.toFixed(2)} ms/秒，占主线程 ${(at100 / 10).toFixed(2)}%\n` +
      `当前实际速率：${currentRate} 条/秒`,
    'running'
  );
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
