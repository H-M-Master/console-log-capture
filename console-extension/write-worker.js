// 写文件工作线程。
//
// 实测 createWritable() 写用户目录会随文件变大越来越慢（1ms → 1s+），
// 因为它用交换文件，每次提交都要处理整份已有内容。createSyncAccessHandle()
// 只对源私有文件系统（OPFS）可用，用户手选的目录用不了。
//
// 所以实时写入全部进 OPFS（O(1) 追加），再定期 / 结束时一次性拷到用户目录。
// 拷贝发生在本线程，尖峰不再打到侧边栏或游戏页面。
//
// 协议：
//   收 { type: 'open', dirHandle, fileName }  -> 回 { type: 'opened', mode }
//   收 { type: 'write', text }                -> 不回
//   收 { type: 'close' }                      -> 回 { type: 'closed', stats }
//   收 { type: 'stats' }                      -> 回 { type: 'stats', stats }
//   错                                      -> 回 { type: 'error', message }

let syncHandle = null;
let writable = null;
let destDirHandle = null;
let destFileName = null;
let opfsDirHandle = null;
let opfsFileHandle = null;
let writeOffset = 0;
let lastCopiedSize = -1;
let mode = 'none';
let exportTimer = null;

const encoder = new TextEncoder();
const EXPORT_MS = 30000;

const queue = [];
let draining = false;
let drainPromise = null;
let exporting = false;
let exportPromise = null;

const stats = {
  calls: 0,
  bytes: 0,
  totalMs: 0,
  maxMs: 0,
  queuePeak: 0,
  errors: 0,
  exportMs: 0,
  exportBytes: 0,
};

function resetStats() {
  stats.calls = 0;
  stats.bytes = 0;
  stats.totalMs = 0;
  stats.maxMs = 0;
  stats.queuePeak = 0;
  stats.errors = 0;
  stats.exportMs = 0;
  stats.exportBytes = 0;
}

async function openOpfs(fileName) {
  const root = await navigator.storage.getDirectory();
  opfsDirHandle = await root.getDirectoryHandle('console-capture', { create: true });
  opfsFileHandle = await opfsDirHandle.getFileHandle(fileName, { create: true });
  syncHandle = await opfsFileHandle.createSyncAccessHandle();
  syncHandle.truncate(0);
  writeOffset = 0;
  lastCopiedSize = -1;
  mode = 'opfs';
}

async function openStream(dirHandle, fileName) {
  const fileHandle = await dirHandle.getFileHandle(fileName, { create: true });
  writable = await fileHandle.createWritable({ keepExistingData: false });
  mode = 'stream';
}

async function open(dirHandle, fileName) {
  destDirHandle = dirHandle;
  destFileName = fileName;
  syncHandle = null;
  writable = null;
  opfsFileHandle = null;
  opfsDirHandle = null;

  try {
    await openOpfs(fileName);
    startExportTimer();
    return mode;
  } catch (e) {
    try {
      if (syncHandle) {
        syncHandle.close();
        syncHandle = null;
      }
    } catch (e2) {}
    opfsFileHandle = null;
    opfsDirHandle = null;
  }

  await openStream(dirHandle, fileName);
  return mode;
}

function startExportTimer() {
  stopExportTimer();
  exportTimer = setInterval(() => {
    exportToDest(true);
  }, EXPORT_MS);
}

function stopExportTimer() {
  if (exportTimer) {
    clearInterval(exportTimer);
    exportTimer = null;
  }
}

function enqueue(text) {
  queue.push(text);
  if (queue.length > stats.queuePeak) stats.queuePeak = queue.length;
  if (!exporting) drain();
}

function drain() {
  if (draining) return drainPromise;
  draining = true;
  drainPromise = (async () => {
    try {
      while (queue.length > 0) {
        const payload = queue.join('');
        queue.length = 0;
        const bytes = encoder.encode(payload);

        const t0 = performance.now();
        try {
          if (syncHandle) {
            syncHandle.write(bytes, { at: writeOffset });
            writeOffset += bytes.byteLength;
          } else if (writable) {
            await writable.write(bytes);
          } else {
            break;
          }
        } catch (e) {
          stats.errors++;
          self.postMessage({ type: 'error', message: '写入失败：' + e.message });
        }
        const ms = performance.now() - t0;
        stats.calls++;
        stats.bytes += bytes.byteLength;
        stats.totalMs += ms;
        if (ms > stats.maxMs) stats.maxMs = ms;
      }
    } finally {
      draining = false;
      drainPromise = null;
    }
  })();
  return drainPromise;
}

function exportToDest(reopen) {
  if (mode !== 'opfs' || !destDirHandle || !opfsFileHandle) return Promise.resolve();
  if (exporting) return exportPromise || Promise.resolve();
  exporting = true;
  exportPromise = (async () => {
    try {
      if (drainPromise) await drainPromise;
      await drain();
      if (writeOffset === lastCopiedSize) return;

      const t0 = performance.now();
      if (syncHandle) {
        syncHandle.flush();
        syncHandle.close();
        syncHandle = null;
      }

      const blob = await opfsFileHandle.getFile();
      const dest = await destDirHandle.getFileHandle(destFileName, { create: true });
      const w = await dest.createWritable({ keepExistingData: false });
      await blob.stream().pipeTo(w);
      lastCopiedSize = blob.size;
      stats.exportMs += performance.now() - t0;
      stats.exportBytes += blob.size;

      if (reopen) {
        syncHandle = await opfsFileHandle.createSyncAccessHandle();
        writeOffset = syncHandle.getSize();
      }
    } catch (e) {
      stats.errors++;
      self.postMessage({ type: 'error', message: '拷贝到用户目录失败：' + e.message });
    } finally {
      exporting = false;
      exportPromise = null;
      drain();
    }
  })();
  return exportPromise;
}

async function close() {
  stopExportTimer();
  if (exportPromise) await exportPromise;
  await drain();
  try {
    if (mode === 'opfs') {
      await exportToDest(false);
    }
    if (syncHandle) {
      syncHandle.flush();
      syncHandle.close();
      syncHandle = null;
    }
    if (writable) {
      await writable.close();
      writable = null;
    }
  } catch (e) {
    stats.errors++;
    self.postMessage({ type: 'error', message: '关闭文件失败：' + e.message });
  }
  mode = 'none';
}

self.onmessage = async (e) => {
  const msg = e.data;
  if (!msg) return;

  try {
    if (msg.type === 'open') {
      resetStats();
      const m = await open(msg.dirHandle, msg.fileName);
      self.postMessage({ type: 'opened', mode: m });
      return;
    }

    if (msg.type === 'write') {
      enqueue(msg.text);
      return;
    }

    if (msg.type === 'stats') {
      const snapshot = { ...stats, mode, queued: queue.length, offset: writeOffset };
      resetStats();
      self.postMessage({ type: 'stats', stats: snapshot });
      return;
    }

    if (msg.type === 'close') {
      await close();
      self.postMessage({ type: 'closed', stats: { ...stats } });
      return;
    }
  } catch (err) {
    self.postMessage({ type: 'error', message: err.message });
  }
};
