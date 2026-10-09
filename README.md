# m3u8 分片下载合并的工具

m3u8 Segment Downloader & Merger

将 HLS（HTTP Live Streaming）播放列表中的离散分片（segment）下载、按需解密，并按播放顺序合并为**单一可播放的 `.ts` 文件**。

- 支持 master playlist 与 media playlist 两层结构，自动列出各清晰度档位（BANDWIDTH / RESOLUTION）。
- 支持 AES-128 加密分片解密（密钥请求一次后缓存，IV 缺省取媒体序列号）。
- 支持 fMP4（EXT-X-MAP 初始化段）。
- 并发下载（可配置线程数），带重试与进度显示。
- 提供命令行版与图形界面版（Tkinter）。
- 可附加 `Referer` / `Cookie` / `User-Agent` 请求头，适配需鉴权的源。

> 使用边界：本工具仅可用于你**有权访问**的视频源。下载、录制、复制行为均受相关著作权法规约束，使用人须自行确认授权状态并承担相应责任。

## 文件结构

```
m3u8-segment-merger/
├── hls_download.py        # 命令行版
├── hls_download_gui.py    # 图形界面版（Tkinter）
├── requirements.txt       # Python 依赖
├── LICENSE
└── README.md
```

两个脚本放在同一目录；图形界面版会 `import hls_download` 复用其解析与解密逻辑。

## 安装

需要 Python 3.10 及以上。

```bash
pip install -r requirements.txt
```

- `requests`、`pycryptodome` 为功能依赖。
- 图形界面版依赖 Tkinter：常规 CPython 发行版自带；部分精简构建（如 python-build-standalone）不含，需要使用自带 Tk 的 Python。命令行版不依赖 Tkinter。

## 命令行版

```bash
# 列出清晰度档位（不下载）
python hls_download.py <m3u8地址> --list

# 下载为 output.ts，自动选最高带宽档
python hls_download.py <m3u8地址> -o output.ts

# 指定清晰度档位、并发数与鉴权请求头
python hls_download.py <m3u8地址> -o output.ts \
    --variant 2 --workers 8 \
    --referer https://example.com/ --cookie "session=xxxx"
```

参数说明：

| 参数 | 说明 |
|------|------|
| `url` | m3u8 地址（master 或 media 层均可） |
| `-o, --output` | 输出文件名，默认 `output.ts` |
| `--referer` | 附加 Referer 请求头 |
| `--cookie` | 附加 Cookie 请求头 |
| `--ua` | 自定义 User-Agent |
| `--variant` | 清晰度档位序号（0 起）；缺省自动选最高带宽 |
| `--workers` | 并发下载线程数，默认 8 |
| `--list` | 仅列出清晰度档位，不下载 |

## 图形界面版

```bash
python hls_download_gui.py
```

操作：填入 m3u8 地址 → 点「检测清晰度」→ 选择档位（默认最高带宽）→ 设定输出文件与可选请求头 → 点「开始下载」。进度条与日志实时显示，「取消」可中断并自动清理临时分片。

## 输出与后续处理

输出的 `.ts` 多数播放器可直接打开。需转封装为 `.mp4`（不重新编码）：

```bash
ffmpeg -i output.ts -c copy output.mp4
```

## 原理简述

HLS 以 index.m3u8 为索引：master playlist 列出各清晰度子流地址，media playlist 列出按时间排序的分片文件名。播放器逐段拉取分片交给解码器。本工具在客户端侧完成同样的拉取与顺序拼合，对 AES-128 加密分片用从 `#EXT-X-KEY` 声明的密钥 URI 取回的 16 字节密钥做 CBC 解密。
