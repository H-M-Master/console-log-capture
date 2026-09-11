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

const MAX_LINES = 5000;
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
let rawEntries = []; // { level, category, text, time }
let totalCount = 0;
let running = false;
let autoScroll = true;
let currentSessionTs = null;
let categoryCounts = { verbose: 0, info: 0, warnings: 0, errors: 0 };
let errorSummaryLines = [];

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

// 只显示某个级别（点击统计条上的分类时用）
function showOnlyCategory(category) {
  levelCheckboxes.forEach((cb) => {
    cb.checked = cb.dataset.level === category;
  });
  filterAllEl.checked = false;
  renderLog();
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

function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = kind;
}

function renderTabInfo(tab) {
  tabInfoEl.textContent = tab ? `目标标签页：${tab.title}\n${tab.url}` : '尚未选择标签页';
}

function formatLine(entry) {
  const time = new Date(entry.time).toISOString();
  return `[${time}] [${entry.level}] ${entry.text}`;
}

// 把连续且内容相同（同分类+同文本）的条目合并成一条，附带次数
function buildDisplayGroups(entries) {
  const groups = [];
  for (const e of entries) {
    const last = groups[groups.length - 1];
    if (last && last.category === e.category && last.text === e.text && last.level === e.level) {
      last.count++;
      last.time = e.time;
    } else {
      groups.push({ level: e.level, category: e.category, text: e.text, time: e.time, count: 1 });
    }
  }
  return groups;
}

function renderLog() {
  const activeCats = new Set(activeCategories());
  const term = searchInput.value.trim().toLowerCase();

  let filtered = rawEntries.filter((e) => activeCats.has(e.category));
  if (term) {
    filtered = filtered.filter((e) => e.text.toLowerCase().includes(term));
  }
  const groups = mergeToggle.checked ? buildDisplayGroups(filtered) : filtered.map((e) => ({ ...e, count: 1 }));

  const frag = document.createDocumentFragment();
  for (const g of groups) {
    const div = document.createElement('div');
    div.className = 'log-line log-' + g.category;
    div.textContent = formatLine(g) + (g.count > 1 ? ` (×${g.count})` : '');
    frag.appendChild(div);
  }
  logViewEl.replaceChildren(frag);

  if (autoScroll) {
    logViewEl.scrollTop = logViewEl.scrollHeight;
    jumpBottomBtn.style.display = 'none';
  }

  const suffix = mergeToggle.checked ? '（已合并重复）' : '';
  lineCountEl.textContent = `共 ${totalCount} 条，当前显示 ${groups.length} 条${suffix}`;
  updateFilterSummary();
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

// ---------- 注入到页面的采集脚本 ----------

// MAIN world：覆写 console 方法，缓冲后通过 postMessage 打包发出
function mainWorldCapture() {
  if (window.__ccStarted) return;
  window.__ccStarted = true;
  window.__ccBuffer = window.__ccBuffer || [];

  function safeStringify(a) {
    try {
      if (typeof a === 'string') return a;
      if (a instanceof Error) return a.stack || a.message;
      return JSON.stringify(a);
    } catch (e) {
      return String(a);
    }
  }

  const methods = ['log', 'warn', 'error', 'info', 'debug'];
  methods.forEach((m) => {
    const orig = console[m];
    console[m] = function (...args) {
      orig.apply(console, args);
      window.__ccBuffer.push({ level: m, text: args.map(safeStringify).join(' '), time: Date.now() });
    };
  });

  window.addEventListener('error', (e) => {
    window.__ccBuffer.push({
      level: 'uncaught-exception',
      text: (e.error && e.error.stack) || e.message,
      time: Date.now(),
    });
  });

  window.addEventListener('unhandledrejection', (e) => {
    window.__ccBuffer.push({ level: 'unhandled-rejection', text: String(e.reason), time: Date.now() });
  });

  const timer = setInterval(() => {
    if (!window.__ccStarted) {
      clearInterval(timer);
      return;
    }
    if (window.__ccBuffer.length === 0) return;
    const batch = window.__ccBuffer.splice(0, window.__ccBuffer.length);
    window.postMessage({ __ccBatch: true, batch }, '*');
  }, 300);
}

// ISOLATED world（默认）：把 MAIN world 的 postMessage 转发给扩展的 runtime 消息
function bridgeInject() {
  if (window.__ccBridgeInstalled) return;
  window.__ccBridgeInstalled = true;
  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (data && data.__ccBatch) {
      try {
        chrome.runtime.sendMessage({ type: 'cc-log-batch', batch: data.batch });
      } catch (e) {
        // 插件重新加载/更新后，旧的扩展上下文会失效（Extension context invalidated），
        // 这里静默忽略，避免污染页面自己的 console
      }
    }
  });
}

function stopMainWorld() {
  window.__ccStarted = false;
}

// ---------- 批次落地：展示 + 落盘 ----------
async function appendBatch(batch) {
  const fileLines = [];
  for (const entry of batch) {
    const category = LEVEL_TO_CATEGORY[entry.level] || 'info';
    rawEntries.push({ ...entry, category });
    const line = formatLine(entry);
    fileLines.push(line);
    categoryCounts[category]++;
    if (category === 'errors') {
      errorSummaryLines.push(line);
    }
  }
  totalCount += batch.length;
  if (rawEntries.length > MAX_LINES) {
    rawEntries = rawEntries.slice(rawEntries.length - MAX_LINES);
  }
  updateStatsBar();
  renderLog();

  if (writable) {
    try {
      await writable.write(fileLines.join('\n') + '\n');
    } catch (e) {
      setStatus('写入本地文件失败：' + e.message, 'error');
    }
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'cc-log-batch' && running) {
    appendBatch(msg.batch);
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
    rawEntries = [];
    totalCount = 0;
    categoryCounts = { verbose: 0, info: 0, warnings: 0, errors: 0 };
    errorSummaryLines = [];
    updateStatsBar();
    renderLog();

    running = true;
    startBtn.disabled = true;
    stopBtn.disabled = false;
    changeDirBtn.disabled = true;
    setStatus(`采集中 → ${fileHandle.name}`, 'running');
  } catch (e) {
    setStatus('开始失败：' + e.message, 'error');
  }
});

async function stopCapture(reason) {
  running = false;
  if (targetTabId != null) {
    try {
      await chrome.scripting.executeScript({ target: { tabId: targetTabId }, world: 'MAIN', func: stopMainWorld });
    } catch (e) {
      // 标签页可能已关闭，忽略
    }
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
      errorFileNote = `\n错误汇总：${errFileHandle.name}（共 ${errorSummaryLines.length} 条）`;
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
  rawEntries = [];
  renderLog();
});

// ---------- 级别筛选 / 搜索 / 合并 ----------
levelCheckboxes.forEach((cb) => {
  cb.addEventListener('change', () => {
    filterAllEl.checked = levelCheckboxes.every((c) => c.checked);
    renderLog();
  });
});

filterAllEl.addEventListener('change', () => {
  levelCheckboxes.forEach((cb) => {
    cb.checked = filterAllEl.checked;
  });
  renderLog();
});

searchInput.addEventListener('input', () => renderLog());
mergeToggle.addEventListener('change', () => renderLog());

// ---------- 统计条 / 错误跳转 ----------
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
