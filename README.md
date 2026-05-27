![Logo](tubetube/static/tubetube.png)


**TubeTube** is a simple YouTube downloader.


## Features:
- **Multithreaded Downloads:** Fast, simultaneous downloads.
- **Custom Locations & Formats:** YAML-based settings.
- **Mobile Optimized:** Designed for small screens.
- **Download Options:** Choose between audio or video.
- **Live Video Support:** Supports multiple live streams.


## Docker Compose Configuration

Create a `docker-compose.yml` file:

```yaml
services:
  tubetube:
    image: ghcr.io/mattblackonly/tubetube:latest
    container_name: tubetube
    ports:
      - 6543:6543
    volumes:
      - /path/to/general:/data/General
      - /path/to/music:/data/Music
      - /path/to/podcasts:/data/Podcast
      - /path/to/videos:/data/Video
      - /path/to/config:/config
      - /path/to/temp:/temp # Optional. Temp files are deleted on startup.
      - /etc/localtime:/etc/localtime:ro # Optional. Sync time with host.
      - /etc/timezone:/etc/timezone:ro # Optional. Sync timezone with host.
    environment:
      - PUID=1000
      - PGID=1000
    restart: unless-stopped
```


## Directory Configuration

Create a `settings.yaml` file in the `/path/to/config` directory with the following format:

```yaml
General:
  audio_ext: m4a
  audio_format_id: '140'
  video_ext: mp4
  video_format_id: '625'
Music:
  audio_ext: mp3
  audio_format_id: '140'
Podcast:
  audio_ext: m4a
  audio_format_id: '140'
Video:
  audio_format_id: '140'
  video_ext: mp4
  video_format_id: '625'

```


### Notes:

- Replace `/path/to/general`, etc.. with actual paths on your host machine.
- Ensure the `settings.yaml` file is correctly placed in the `/path/to/config` directory.
- The volume paths in the `docker-compose.yml` file should match the names specified in the settings.yaml file (e.g., /data/**General**, etc..).
- You can create as many directory locations as needed in `settings.yaml`, but each must be mapped individually in `docker-compose.yml`.
- To use a cookies file, create a `cookies.txt` file and place it in the config directory.

#### Subtitle Configuration

- When `WRITE_SUBS=True`, actual subtitles will be saved to a subtitle file. If no actual subtitles are available, no subtitles will be created. Additionally, setting `ALLOW_AUTO_SUBS=True` provides a fallback to automatically generated subtitles saved to the subtitle file.
- When `EMBED_SUBS=True`, actual subtitles will be embedded into the video. If no actual subtitles are present, no subtitles will be included. Similarly, `ALLOW_AUTO_SUBS=True` can serve as a fallback to embed automatically generated subtitles.

To effectively manage subtitles, enable `ALLOW_AUTO_SUBS` in conjunction with either `WRITE_SUBS` or `EMBED_SUBS`. This configuration will attempt to download actual subtitles, and if they are not available, it will default to using automatically generated subtitles.

## Configuration via Environment Variables

Customize the behavior of **TubeTube** by setting the following environment variables in your `docker-compose.yml` file:

```yaml
environment:
  - PUID=1000                       # 用户ID（默认: 1000）
  - PGID=1000                       # 用户组ID（默认: 1000）
  - VERBOSE_LOGS=false              # 启用 yt-dlp 详细日志（默认: false）
  - TRIM_METADATA=false             # 修剪文件元数据（默认: false）
  - PROXY=http://127.0.0.1:7897     # 代理地址（可选）
  - JS_RUNTIMES=node,deno           # JS 运行时列表（默认: 自动检测）
  - PREFERRED_LANGUAGE=en           # 下载音频的首选语言（默认: en）
  - PREFERRED_AUDIO_CODEC=aac       # 首选音频编码（默认: aac）
  - PREFERRED_VIDEO_CODEC=vp9       # 首选视频编码（默认: vp9）
  - PREFERRED_VIDEO_EXT=mp4         # 首选视频扩展名（默认: mp4）
  - EMBED_SUBS=false                # 将字幕内嵌到视频中（默认: false）
  - WRITE_SUBS=false                # 将字幕保存为独立文件（默认: false）
  - ALLOW_AUTO_SUBS=false           # 允许自动生成字幕作为兜底（默认: true）
  - SUBTITLE_FORMAT=vtt             # 字幕格式（默认: vtt）
  - SUBTITLE_LANGUAGES=en           # 字幕语言（默认: en）
  - THREAD_COUNT=4                  # 处理线程数量（默认: 4）
```
> 注： yt‑dlp 对 Node 的最低支持是 v20

本地开发可使用 `config/app_config.yaml` 配置；如存在环境变量则优先生效。

## Screenshots

### Phone (Dark Mode)

![Phone](tubetube/static/phone-screenshot.png)



### Desktop (Dark Mode)

![Screenshot](tubetube/static/screenshot.png)




## 注意事项

下载最好携带 cookie，一个视频对应一个 cookie，当遇到如下错误提示，需要重试即可（前提是该视频的cookie没有下载成功过，一旦下载过视频就需要更换新的cookie）

```bash
[download] Got error: [SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC] decryption failed or bad record mac (_ssl.c:2580)
[download] Got error: [SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC] decryption failed or bad record mac (_ssl.c:2580)
```

## 常用命令

### yt-dlp

```bash

# 查看分辨率
yt-dlp --cookies ./cookies.txt --js-runtimes node -F <url>

# 查看字幕
yt-dlp --cookies ./cookies.txt --list-subs <url>

# 只下载字幕
yt-dlp --cookies ./cookies.txt \
		   --js-runtimes node \
	     --skip-download \
       --write-subs \
       --sub-langs zh-TW \
       --sub-format srt <url>
```

### 同步代码

```git
git fetch --all
git merge upstream/main
```