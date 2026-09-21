// 写文件工作线程。
//
// 为什么需要它：实测 FileSystemWritableFileStream.write() 的耗时极不稳定——
// 中位数只有 2ms，但会偶发飙到 300ms / 700ms / 2400ms，而且和数据量无关
// （2442ms 那次只写了 3.3KB，而 45KB 那次只花 4ms）。这是 Chrome 在做文件系统
// 提交时被系统 I/O 阻塞。只要它发生在主线程上，整个面板和浏览器就会跟着卡。
//
// 解决办法是把文件 I/O 整个搬到这个 worker 里：
//   - 优先用 createSyncAccessHandle()，它是专为 worker 设计的高性能同步写入接口，
//     没有 createWritable() 那套交换文件 + 提交的开销；
//   - 不支持时退回 createWritable()，此时虽然仍可能有尖峰，但卡的是 worker 线程，
//     主线程照常渲染，游戏也不受影响。
//
// 与主线程的约定（postMessage）：
//   收 { type: 'open', dirHandle, fileName }        -> 回 { type: 'opened', mode }
//   收 { type: 'write', text }                      -> 不回（异步落盘）
//   收 { type: 'close' }                            -> 回 { type: 'closed', stats }
//   收 { type: 'stats' }                            -> 回 { type: 'stats', stats }
//   任何错误                                         -> 回 { type: 'error', message }

let syncHandle = null; // FileSystemSyncAccessHandle（首选）
let writable = null; // FileSystemWritableFileStream（退路）
let writeOffset = 0;
let mode = 'none';

const encoder = new TextEncoder();

// 待写队列：主线程发来的文本先进队列，由这里串行落盘，
// 保证顺序，也避免并发写互相干扰。
const queue = [];
let draining = false;

// 统计：让主线程能知道真实的落盘开销，而不用自己计时
const stats = {
  calls: 0,
  bytes: 0,
  totalMs: 0,
  maxMs: 0,
  queuePeak: 0,
  errors: 0,
};

function resetStats() {
  stats.calls = 0;
  stats.bytes = 0;
  stats.totalMs = 0;
  stats.maxMs = 0;
  stats.queuePeak = 0;
  stats.errors = 0;
}

async function open(dirHandle, fileName) {
  const fileHandle = await dirHandle.getFileHandle(fileName, { create: true });

  // 首选同步句柄：worker 专属，开销远低于 createWritable
  if (typeof fileHandle.createSyncAccessHandle === 'function') {
    try {
      syncHandle = await fileHandle.createSyncAccessHandle();
      syncHandle.truncate(0);
      writeOffset = 0;
      mode = 'sync';
      return mode;
    } catch (e) {
      // 某些目录（非 OPFS）不支持同步句柄，退回流式写入
      syncHandle = null;
    }
  }

  writable = await fileHandle.createWritable({ keepExistingData: false });
  mode = 'stream';
  return mode;
}

function enqueue(text) {
  queue.push(text);
  if (queue.length > stats.queuePeak) stats.queuePeak = queue.length;
  drain();
}

async function drain() {
  if (draining) return;
  draining = true;
  try {
    while (queue.length > 0) {
      // 一次把队列里攒的全部合并写出，减少调用次数
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
          break; // 还没 open 或已 close，丢弃
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
  }
}

async function close() {
  // 把队列里剩下的写完再关，否则会丢尾部日志
  await drain();
  try {
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
      const snapshot = { ...stats, mode, queued: queue.length };
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
