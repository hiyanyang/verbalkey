"""多线程分片下载。GitHub release 支持 Range，用并发把单连接带宽吃满。

用法: python mget.py <url> <输出文件> [线程数]
"""

from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request

CHUNK = 1 << 20  # 1 MiB


def head_size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(r.headers.get("Content-Length", 0))


def download(url: str, out: str, workers: int = 16) -> None:
    total = head_size(url)
    if total <= 0:
        raise SystemExit("无法获取文件大小")

    tmp = out + ".part"
    with open(tmp, "wb") as f:
        f.truncate(total)

    ranges = []
    step = total // workers
    for i in range(workers):
        start = i * step
        end = total - 1 if i == workers - 1 else (start + step - 1)
        ranges.append((start, end))

    done = [0] * workers
    lock = threading.Lock()
    stop = threading.Event()

    def worker(idx: int, start: int, end: int):
        wrote = 0
        try:
            with open(tmp, "r+b") as f:
                f.seek(start)
                pos = start
                while pos <= end and not stop.is_set():
                    seg_end = min(pos + CHUNK - 1, end)
                    req = urllib.request.Request(url)
                    req.add_header("Range", f"bytes={pos}-{seg_end}")
                    with urllib.request.urlopen(req, timeout=60) as r:
                        data = r.read()
                    if not data:
                        break
                    f.write(data)
                    pos += len(data)
                    wrote += len(data)
                    with lock:
                        done[idx] = wrote
        except Exception as e:
            with lock:
                done[idx] = -1
            stop.set()
            print(f"[线程 {idx}] 失败: {e}", flush=True)

    threads = []
    for i, (s, e) in enumerate(ranges):
        t = threading.Thread(target=worker, args=(i, s, e), daemon=True)
        t.start()
        threads.append(t)

    t0 = time.time()
    last = 0
    while any(t.is_alive() for t in threads):
        time.sleep(1)
        cur = sum(d for d in done if d > 0)
        if any(d < 0 for d in done):
            break
        speed = (cur - last)
        pct = cur / total * 100
        print(f"\r{cur/1048576:.1f}/{total/1048576:.1f} MB  {pct:5.1f}%  "
              f"{speed/1024:.0f} KB/s", end="", flush=True)
        last = cur

    for t in threads:
        t.join(timeout=10)

    if any(d < 0 for d in done) or not stop.is_set() is False:
        pass

    ok = os.path.getsize(tmp) == total and not any(d < 0 for d in done)
    if ok:
        os.replace(tmp, out)
        print(f"\n完成: {out}  ({total/1048576:.1f} MB, {time.time()-t0:.0f}s)")
    else:
        print(f"\n未完成，保留 {tmp} 供续传")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 16
    download(sys.argv[1], sys.argv[2], n)
