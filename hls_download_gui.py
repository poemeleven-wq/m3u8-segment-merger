# -*- coding: utf-8 -*-
"""
hls_download_gui.py — 「m3u8 分片下载合并的工具」图形界面版（仅限你有权访问的源使用）

复用 hls_download.py 的解析与解密逻辑，提供窗口操作：
  粘贴 m3u8 地址 -> 检测清晰度 -> 开始下载 -> 进度与日志实时显示。

运行：
  python hls_download_gui.py

依赖：requests、pycryptodome（Tkinter 为 Python 自带）
"""

import concurrent.futures
import os
import queue
import shutil
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from urllib.parse import urljoin

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hls_download import parse_master, parse_media, DEFAULT_HEADERS  # noqa: E402
from Crypto.Cipher import AES  # noqa: E402

APP_TITLE = "m3u8 分片下载合并的工具（仅限有权访问的源）"

HELP_TEXT = """使用步骤：
  1. 在「m3u8 地址」填入你有权访问的视频播放列表地址（master 或 media 层均可）。
  2. 点「检测清晰度」——多档清晰度时在下方下拉框选择，单一清晰度会直接提示。
  3. 设定输出文件名（默认 output.ts）；如源需要鉴权，填写 Referer / Cookie。
  4. 点「开始下载」。进度条与日志实时更新，完成后可直接用播放器或 ffmpeg 打开 .ts 文件。

说明：
  · .ts 输出可直接播放；需要 mp4 时用 ffmpeg 转封装：ffmpeg -i 输出.ts -c copy 输出.mp4
  · 下载失败时会自动重试 3 次；点「取消」可中断，已下载部分不会保留。
  · 请只对有权访问的视频源使用本工具。
"""


# ---------------------------------------------------------------- 下载核心

class FetchError(Exception):
    pass


def make_session(headers: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update({**DEFAULT_HEADERS, **headers})
    return session


def fetch(session: requests.Session, url: str, retries: int = 3) -> bytes:
    last_err = None
    for _ in range(retries):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as err:
            last_err = err
    raise FetchError(f"{url} -> {last_err}")


def get_key(session: requests.Session, key_uri: str, cache: dict) -> bytes:
    if key_uri not in cache:
        data = fetch(session, key_uri)
        if len(data) != 16:
            raise FetchError(f"密钥长度异常：{len(data)} 字节（应为 16）")
        cache[key_uri] = data
    return cache[key_uri]


def decrypt(data: bytes, key_info, seq: int, session, cache: dict) -> bytes:
    method = key_info.method
    if method == "NONE":
        return data
    if method != "AES-128":
        raise FetchError(f"暂不支持的加密方式：{method}")
    raw_key = get_key(session, key_info.key_uri, cache)
    iv_hex = key_info.iv
    iv = bytes.fromhex(iv_hex.removeprefix("0x")) if iv_hex else seq.to_bytes(16, "big")
    return AES.new(raw_key, AES.MODE_CBC, iv).decrypt(data)


def download_task(url: str, variant_index, headers: dict, workers: int,
                  output_path: str, progress_q: queue.Queue, cancel: threading.Event):
    """后台线程主任务。所有结果经 progress_q 传回 UI 线程。"""
    def post(kind, **kw):
        progress_q.put({"kind": kind, **kw})

    try:
        session = make_session(headers)
        text = fetch(session, url).decode("utf-8", errors="replace")
        variants = parse_master(text, url)

        if variants:
            idx = len(variants) - 1 if variant_index is None else variant_index
            if not 0 <= idx < len(variants):
                raise FetchError(f"清晰度档位 {idx} 不存在（0 ~ {len(variants)-1}）")
            sub_url = variants[idx][2]
            post("log", msg=f"选择档位 [{idx}] {variants[idx][1]} {variants[idx][0]//1000} kbps")
            text = fetch(session, sub_url).decode("utf-8", errors="replace")
            url = sub_url
        elif variant_index is not None:
            post("log", msg="源是单一清晰度，忽略档位选择。")

        segments, key_states, init_segment, media_seq = parse_media(text, url)
        if not segments:
            raise FetchError("media playlist 里没有解析到任何分片。")
        encrypted = sum(1 for k in key_states if k.method != "NONE")
        post("total", total=len(segments), encrypted=encrypted)
        post("log", msg=f"共 {len(segments)} 个分片（加密 {encrypted} 个），开始下载…")

        tmp_dir = tempfile.mkdtemp(prefix="hlsdl_")
        key_cache = {}
        futures = []

        def work(i):
            if cancel.is_set():
                raise FetchError("已取消")
            data = fetch(session, segments[i])
            return i, decrypt(data, key_states[i], media_seq + i, session, key_cache)

        done = 0
        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            futures = [pool.submit(work, i) for i in range(len(segments))]
            for fut in concurrent.futures.as_completed(futures):
                i, data = fut.result()
                with open(os.path.join(tmp_dir, f"{i:06d}.part"), "wb") as fh:
                    fh.write(data)
                done += 1
                post("progress", done=done)
                if cancel.is_set():
                    break

        if cancel.is_set():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            post("cancelled")
            return

        if init_segment:
            with open(os.path.join(tmp_dir, "999999.part"), "wb") as fh:
                fh.write(fetch(session, init_segment))

        with open(output_path, "wb") as out:
            if init_segment:
                out.write(open(os.path.join(tmp_dir, "999999.part"), "rb").read())
            for i in range(len(segments)):
                out.write(open(os.path.join(tmp_dir, f"{i:06d}.part"), "rb").read())
        shutil.rmtree(tmp_dir, ignore_errors=True)

        size_mb = os.path.getsize(output_path) / 1024 / 1024
        post("done", size_mb=size_mb, path=output_path)
    except FetchError as err:
        if cancel.is_set():
            post("cancelled")
        else:
            post("error", msg=str(err))
    except Exception as err:  # noqa: BLE001
        post("error", msg=f"{type(err).__name__}: {err}")


# ---------------------------------------------------------------- 界面

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("760x640")
        root.minsize(680, 560)

        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.variant_count = 0

        self._build_help()
        self._build_inputs()
        self._build_progress()
        self._build_log()
        root.after(100, self._poll_queue)

    # ---- 说明区
    def _build_help(self):
        box = ttk.LabelFrame(self.root, text="使用说明")
        box.pack(fill="x", padx=10, pady=(10, 6))
        txt = tk.Text(box, height=9, wrap="word", relief="flat",
                      background="#F7F7F5", foreground="#333333")
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")
        txt.pack(fill="x", padx=8, pady=6)

    # ---- 输入区
    def _build_inputs(self):
        box = ttk.LabelFrame(self.root, text="参数")
        box.pack(fill="x", padx=10, pady=6)

        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(row, text="m3u8 地址", width=12).pack(side="left")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(row, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.btn_detect = ttk.Button(row, text="检测清晰度", command=self.on_detect)
        self.btn_detect.pack(side="left")

        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="清晰度档位", width=12).pack(side="left")
        self.variant_var = tk.StringVar()
        self.variant_box = ttk.Combobox(row, textvariable=self.variant_var,
                                        state="disabled", width=40)
        self.variant_box.pack(side="left")
        ttk.Label(row, text="（默认选最高带宽）", foreground="#777777").pack(side="left", padx=6)

        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="输出文件", width=12).pack(side="left")
        self.out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "output.ts"))
        ttk.Entry(row, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", command=self.on_browse).pack(side="left", padx=(8, 0))

        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="Referer", width=12).pack(side="left")
        self.referer_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.referer_var).pack(side="left", fill="x", expand=True)
        ttk.Label(row, text="并发数", padding=(16, 0, 6, 0)).pack(side="left")
        self.workers_var = tk.IntVar(value=8)
        ttk.Spinbox(row, from_=1, to=32, textvariable=self.workers_var, width=5).pack(side="left")

        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Label(row, text="Cookie", width=12).pack(side="left")
        self.cookie_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.cookie_var).pack(side="left", fill="x", expand=True)

        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=(0, 10))
        self.btn_start = ttk.Button(row, text="开始下载", command=self.on_start)
        self.btn_start.pack(side="right")
        self.btn_cancel = ttk.Button(row, text="取消", command=self.on_cancel, state="disabled")
        self.btn_cancel.pack(side="right", padx=(0, 8))

    # ---- 进度区
    def _build_progress(self):
        box = ttk.LabelFrame(self.root, text="进度")
        box.pack(fill="x", padx=10, pady=6)
        self.progress = ttk.Progressbar(box, mode="determinate")
        self.progress.pack(fill="x", padx=8, pady=(8, 2))
        self.status_var = tk.StringVar(value="就绪。")
        ttk.Label(box, textvariable=self.status_var).pack(anchor="w", padx=8, pady=(0, 8))

    # ---- 日志区
    def _build_log(self):
        box = ttk.LabelFrame(self.root, text="日志")
        box.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.log_text = tk.Text(box, height=8, wrap="word", state="disabled",
                                background="#111111", foreground="#DDDDDD")
        scroll = ttk.Scrollbar(box, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll.pack(side="right", fill="y", pady=8, padx=(0, 8))

    # ---- 事件
    def log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def on_browse(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".ts", filetypes=[("MPEG-TS 视频", "*.ts"), ("所有文件", "*.*")])
        if path:
            self.out_var.set(path)

    def on_detect(self):
        url = self.url_var.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("提示", "请先填入以 http(s):// 开头的 m3u8 地址。")
            return
        self.btn_detect.configure(state="disabled")
        self.variant_box.configure(state="disabled")
        self.status_var.set("正在检测清晰度…")
        threading.Thread(target=self._detect_task, args=(url,), daemon=True).start()

    def _detect_task(self, url: str):
        try:
            session = make_session({})
            text = fetch(session, url).decode("utf-8", errors="replace")
            variants = parse_master(text, url)
            self.q.put({"kind": "detected", "variants": variants})
        except Exception as err:  # noqa: BLE001
            self.q.put({"kind": "detect_fail", "msg": f"{type(err).__name__}: {err}"})

    def on_start(self):
        url = self.url_var.get().strip()
        out = self.out_var.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("提示", "m3u8 地址不能为空，且需以 http(s):// 开头。")
            return
        if not out:
            messagebox.showwarning("提示", "请设定输出文件。")
            return
        parent = os.path.dirname(os.path.abspath(out))
        if parent and not os.path.isdir(parent):
            messagebox.showwarning("提示", f"输出目录不存在：{parent}")
            return

        variant_index = None
        if self.variant_box.instate(["!disabled"]) and self.variant_box.current() >= 0:
            variant_index = self.variant_box.current()

        headers = {}
        if self.referer_var.get().strip():
            headers["Referer"] = self.referer_var.get().strip()
        if self.cookie_var.get().strip():
            headers["Cookie"] = self.cookie_var.get().strip()

        self.cancel.clear()
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.progress.configure(value=0)
        self.status_var.set("下载中…")
        self.log(f"开始：{url}")
        self.worker = threading.Thread(
            target=download_task,
            args=(url, variant_index, headers, self.workers_var.get(), out, self.q, self.cancel),
            daemon=True)
        self.worker.start()

    def on_cancel(self):
        self.cancel.set()
        self.status_var.set("正在取消…")

    def _finish(self, status_msg: str):
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.status_var.set(status_msg)
        self.worker = None

    # ---- 队列轮询（后台线程 -> UI 的唯一通道）
    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg["kind"]
                if kind == "detected":
                    variants = msg["variants"]
                    self.btn_detect.configure(state="normal")
                    if variants:
                        self.variant_count = len(variants)
                        labels = [f"[{i}] {v[1] or '?'}  {v[0] // 1000} kbps"
                                  for i, v in enumerate(variants)]
                        self.variant_box.configure(values=labels, state="readonly")
                        self.variant_box.current(len(variants) - 1)
                        self.status_var.set(f"检测到 {len(variants)} 档清晰度，已默认选最高档。")
                        self.log(f"清晰度检测完成：{len(variants)} 档。")
                    else:
                        self.variant_box.configure(values=["单一清晰度"], state="readonly")
                        self.variant_box.current(0)
                        self.status_var.set("源为单一清晰度的 media playlist，可直接开始下载。")
                        self.log("清晰度检测完成：单一清晰度。")
                elif kind == "detect_fail":
                    self.btn_detect.configure(state="normal")
                    self.status_var.set("检测失败。")
                    self.log(f"检测失败：{msg['msg']}")
                elif kind == "total":
                    self.progress.configure(maximum=msg["total"], value=0)
                    self.log(f"共 {msg['total']} 个分片（加密 {msg['encrypted']} 个）。")
                elif kind == "progress":
                    self.progress.configure(value=msg["done"])
                elif kind == "log":
                    self.log(msg["msg"])
                elif kind == "done":
                    self.progress.configure(value=self.progress["maximum"])
                    self.log(f"完成：{msg['path']}（{msg['size_mb']:.1f} MB）")
                    self._finish(f"完成：{msg['path']}（{msg['size_mb']:.1f} MB）")
                elif kind == "error":
                    self.log(f"失败：{msg['msg']}")
                    self._finish("失败，详见日志。")
                elif kind == "cancelled":
                    self.log("已取消，临时文件已清理。")
                    self._finish("已取消。")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
