# -*- coding: utf-8 -*-
"""
hls_download.py — 「m3u8 分片下载合并的工具」命令行版（仅限你有权访问的源使用）

功能：
  1. 解析 master playlist，列出各清晰度档位，可选择或自动选最高带宽档
  2. 解析 media playlist，提取分片列表、AES-128 密钥、fMP4 初始化段
  3. 并发下载分片（可配置并发数、Referer / User-Agent / Cookie 等请求头）
  4. 自动解密 AES-128 加密分片（密钥请求一次后缓存；IV 缺省取媒体序列号）
  5. 按序合并输出单个 .ts 文件（多数播放器与 ffmpeg 可直接读取）

用法：
  python hls_download.py <m3u8地址> -o 输出文件.ts
  python hls_download.py <m3u8地址> -o 输出.ts --referer https://example.com/ \
      --cookie "session=xxxx" --variant 2 --workers 8

依赖：requests、pycryptodome
"""

import argparse
import concurrent.futures
import os
import sys
import urllib.parse

import requests
from Crypto.Cipher import AES

# ---------------------------------------------------------------- 基础工具

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}


def url_join(base: str, path: str) -> str:
    """把 playlist 里的相对地址拼成完整 URL。"""
    return urllib.parse.urljoin(base, path)


def fetch(session: requests.Session, url: str, retries: int = 3) -> bytes:
    """带重试的 GET，返回响应体。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as err:
            last_err = err
            sys.stderr.write(f"  [重试 {attempt}/{retries}] {url} -> {err}\n")
    raise RuntimeError(f"请求最终失败：{url}") from last_err


# ---------------------------------------------------------------- playlist 解析

def parse_master(text: str, base_url: str):
    """解析 master playlist，返回 [(带宽, 分辨率, 子playlist绝对URL), ...]。"""
    variants = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            # 该标签的下一行非注释行就是子 playlist 地址
            for follow in lines[i + 1:]:
                follow = follow.strip()
                if follow and not follow.startswith("#"):
                    variants.append((
                        int(attrs.get("BANDWIDTH", 0)),
                        attrs.get("RESOLUTION", ""),
                        url_join(base_url, follow),
                    ))
                    break
    return variants


def parse_attrs(attr_str: str) -> dict:
    """解析 HLS 属性串：KEY=VALUE,KEY="VALUE" -> dict。"""
    attrs, key, buf, in_quote = {}, "", "", False
    for ch in attr_str:
        if ch == '"':
            in_quote = not in_quote
        elif ch == ',' and not in_quote:
            k, _, v = buf.partition("=")
            attrs[k.strip()] = v.strip()
            buf = ""
        else:
            buf += ch
    if buf:
        k, _, v = buf.partition("=")
        attrs[k.strip()] = v.strip()
    return attrs


class KeyInfo:
    """一条 EXT-X-KEY 声明。METHOD=NONE 表示此后分片未加密。"""
    __slots__ = ("method", "key_uri", "iv")

    def __init__(self, method: str, key_uri: str | None, iv: str | None):
        self.method = method
        self.key_uri = key_uri
        self.iv = iv


def parse_media(text: str, base_url: str):
    """解析 media playlist，返回 (分片URL列表, 密钥状态列表, 初始化段URL或None, 媒体序列号)。

    密钥状态列表与分片一一对应：每个元素是 KeyInfo，记录该分片应使用的加密方式。
    """
    segments, key_states = [], []
    current_key = KeyInfo("NONE", None, None)
    init_segment = None
    media_sequence = 0

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE")
            uri = attrs.get("URI")
            current_key = KeyInfo(
                method=method,
                key_uri=url_join(base_url, uri) if uri else None,
                iv=attrs.get("IV"),
            )
        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            if "URI" in attrs:
                init_segment = url_join(base_url, attrs["URI"])
        elif line and not line.startswith("#"):
            segments.append(url_join(base_url, line))
            key_states.append(current_key)

    return segments, key_states, init_segment, media_sequence


# ---------------------------------------------------------------- 下载与解密

class Downloader:
    def __init__(self, headers: dict, workers: int):
        self.session = requests.Session()
        self.session.headers.update({**DEFAULT_HEADERS, **headers})
        self.workers = workers
        self._key_cache = {}

    def get_key(self, key_uri: str) -> bytes:
        """取 AES-128 密钥，按 URI 缓存。"""
        if key_uri not in self._key_cache:
            data = fetch(self.session, key_uri)
            if len(data) != 16:
                raise RuntimeError(f"密钥长度异常：{len(data)} 字节（应为 16）")
            self._key_cache[key_uri] = data
        return self._key_cache[key_uri]

    def decrypt(self, data: bytes, key: KeyInfo, seq: int) -> bytes:
        """解密单个分片。METHOD=NONE 原样返回；AES-128 用 CBC 模式。"""
        if key.method == "NONE":
            return data
        if key.method != "AES-128":
            raise RuntimeError(f"暂不支持的加密方式：{key.method}")
        raw_key = self.get_key(key.key_uri)
        if key.iv:
            iv = bytes.fromhex(key.iv.removeprefix("0x"))
        else:
            # 规范缺省：IV 取媒体序列号的 16 字节大端表示
            iv = seq.to_bytes(16, "big")
        cipher = AES.new(raw_key, AES.MODE_CBC, iv)
        return cipher.decrypt(data)

    def download_all(self, jobs: list) -> list:
        """并发下载。jobs: [(序号, URL, KeyInfo, 序列号), ...]，返回按序号排列的内容列表。"""
        results = [None] * len(jobs)
        done = 0

        def work(job):
            idx, url, key, seq = job
            data = fetch(self.session, url)
            return idx, self.decrypt(data, key, seq)

        with concurrent.futures.ThreadPoolExecutor(self.workers) as pool:
            for idx, data in pool.map(work, jobs):
                results[idx] = data
                done += 1
                if done % 20 == 0 or done == len(jobs):
                    sys.stderr.write(f"\r  进度 {done}/{len(jobs)} 个分片")
                    sys.stderr.flush()
        sys.stderr.write("\n")
        return results


# ---------------------------------------------------------------- 主流程

def choose_variant(variants: list, want: int | None):
    """选择清晰度档位。want 为 None 时自动选带宽最高的一档。"""
    if not variants:
        return None
    if want is None:
        return max(variants, key=lambda v: v[0])
    return variants[want]


def main():
    parser = argparse.ArgumentParser(description="HLS 下载脚本（仅限有权访问的源）")
    parser.add_argument("url", help="m3u8 地址（master 或 media 均可）")
    parser.add_argument("-o", "--output", default="output.ts", help="输出文件名")
    parser.add_argument("--referer", default=None, help="Referer 请求头")
    parser.add_argument("--cookie", default=None, help="Cookie 请求头")
    parser.add_argument("--ua", default=None, help="自定义 User-Agent")
    parser.add_argument("--variant", type=int, default=None,
                        help="清晰度档位序号（0 起），缺省自动选最高带宽")
    parser.add_argument("--workers", type=int, default=8, help="并发下载数")
    parser.add_argument("--list", action="store_true",
                        help="只列出可用清晰度档位，不下载")
    args = parser.parse_args()

    headers = {}
    if args.referer:
        headers["Referer"] = args.referer
    if args.cookie:
        headers["Cookie"] = args.cookie
    if args.ua:
        headers["User-Agent"] = args.ua

    dl = Downloader(headers, args.workers)
    playlist_url = args.url

    # 第一步：取 playlist。若内容含 EXT-X-STREAM-INF 则是 master，需要再下一层。
    text = fetch(dl.session, playlist_url).decode("utf-8", errors="replace")
    variants = parse_master(text, playlist_url)

    if variants:
        print(f"master playlist，共 {len(variants)} 档清晰度：")
        for i, (bw, res, _u) in enumerate(variants):
            print(f"  [{i}] {res or '?':>11}  {bw / 1000:.0f} kbps")
        if args.list:
            return
        chosen = choose_variant(variants, args.variant)
        if chosen is None:
            sys.exit("master playlist 里没有找到任何子流，无法继续。")
        print(f"选择档位 [{variants.index(chosen)}] -> {chosen[2]}")
        text = fetch(dl.session, chosen[2]).decode("utf-8", errors="replace")
        playlist_url = chosen[2]
    elif args.list:
        print("这是 media playlist（单一清晰度），无档位可列。")
        return

    # 第二步：解析分片与加密信息
    segments, key_states, init_segment, media_seq = parse_media(text, playlist_url)
    if not segments:
        sys.exit("media playlist 里没有解析到任何分片。")
    encrypted = sum(1 for k in key_states if k.method != "NONE")
    print(f"分片数 {len(segments)}，其中加密分片 {encrypted} 个。")

    jobs = [(i, url, key, media_seq + i) for i, (url, key) in enumerate(zip(segments, key_states))]

    # 第三步：并发下载 + 解密
    contents = dl.download_all(jobs)
    if any(c is None for c in contents):
        failed = [jobs[i][1] for i, c in enumerate(contents) if c is None]
        sys.exit(f"有 {len(failed)} 个分片下载失败，中止合并。")

    # 第四步：按序合并（fMP4 先写初始化段）
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "wb") as fh:
        if init_segment:
            fh.write(fetch(dl.session, init_segment))
            print("已写入 fMP4 初始化段。")
        for chunk in contents:
            fh.write(chunk)

    size_mb = os.path.getsize(args.output) / 1024 / 1024
    print(f"完成：{args.output}（{size_mb:.1f} MB）")


if __name__ == "__main__":
    main()
