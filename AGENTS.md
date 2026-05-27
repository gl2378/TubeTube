• 仓库结构总览

  - 顶层布局（关键目录/文件）
      - tubetube/：后端与前端静态资源（核心代码）
      - config/：默认 YAML 配置样例（运行时会被覆盖/重写）
      - scripts/：离线脚本工具（独立 CLI，不属于主服务）
      - start.sh / start_config.py：容器启动入口与 gunicorn 配置
      - Dockerfile / requirements.txt / requirements_def.txt：运行环境与依赖（前者为浮动版本，后者为版本钉死参考）
      - data/：默认数据输出目录（运行时写入）
      - README.md / LICENSE / AGENTS.md：文档与本索引
  - 入口（运行方式）
      - 容器/生产：start.sh → gunicorn tubetube.tubetube:app -c start_config.py（监听 0.0.0.0:6543）
      - 直接运行：tubetube/tubetube.py 中 web_app.run_app()（监听 0.0.0.0:8500）
          - 注意：tubetube.py 顶层导入用 `from settings import …`、`from yt_downloader import …`，
            依赖 PYTHONPATH 指向 tubetube/。本地直跑请用：
            `PYTHONPATH=tubetube python tubetube/tubetube.py`，或先 `cd tubetube`。
      - 工具脚本（不是主服务入口）
          - scripts/process_vtt.py：基于 tubetube/vtt_tool.py 提取 VTT 文本，调用 OpenCC（t2s）做繁→简，再按 80~120 字断段
          - scripts/transcribe_mp4_with_whisper.py：基于 OpenAI Whisper 把 MP4/MP3 转写为 .txt；MP4 输入时会先用 ffmpeg 抽出同名 .mp3 复用，默认输入 data/Video/test.mp4、模型 large、语言 zh
  - 核心模块（职责）
      - tubetube/tubetube.py：Flask + Socket.IO 网页入口、事件路由；WebApp 多重继承 Settings 与 DownloadManager
        （Settings 必须先初始化，给 DownloadManager 提供 config_folder / cookies_file）
      - tubetube/settings.py：配置路径解析、settings.yaml 读取与默认落盘、文件夹分类与创建；定义 DownloadCancelledException
      - tubetube/yt_downloader.py：下载队列、线程池、yt‑dlp 参数构建、进度回传、取消/移除逻辑、临时目录清理
      - tubetube/helpers.py：URL 解析（parse_video_id 提取 YouTube 视频 ID）、yt‑dlp 后处理器（TrimDescriptionPP 把 description 裁到 250 字符，仅在 TRIM_METADATA=true 时注入）
      - tubetube/vtt_tool.py：VttSubtitleTool，VTT 字幕解析与清洗（供脚本使用）
      - tubetube/templates/index.html：前端单页骨架（Bootstrap 5.3 + Bootstrap Icons + Socket.IO 4.7 CDN）
      - tubetube/static/：
          - js_general_script.js：全局逻辑与 Socket.IO 客户端
          - js_table_script.js：活动表渲染与行选择/移除
          - js_theme_switcher.js：Day/Auto/Night 主题切换
          - style.css、logo.png、tubetube.png、screenshot.png、phone-screenshot.png
  - 依赖关系（内部 + 外部）
      - 内部关系
          - tubetube/tubetube.py → tubetube/settings.py + tubetube/yt_downloader.py
          - tubetube/yt_downloader.py → tubetube/helpers.py + tubetube/settings.py（DownloadCancelledException）
          - scripts/process_vtt.py → tubetube/vtt_tool.py（+ 可选 opencc）
          - scripts/transcribe_mp4_with_whisper.py → openai-whisper + 系统 ffmpeg（与主服务无依赖）
      - 配置/数据流
          - Settings 优先使用 TUBETUBE_CONFIG_DIR / TUBETUBE_DATA_DIR，否则优先 /config、/data，再回退到仓库内
            config/、data/
          - DownloadManager 从 TUBETUBE_APP_CONFIG 或 <config_folder>/app_config.yaml 读配置；环境变量同名键覆盖
          - cookies 文件：<config_folder>/cookies.txt 存在则自动注入 yt‑dlp 的 cookiefile
      - 关键第三方
          - 后端：flask, flask_socketio, gunicorn, gevent, gevent-websocket
          - 下载：yt_dlp[default]
          - 配置：pyyaml
          - 字幕处理（脚本可选）：opencc-python-reimplemented
          - 离线转写（脚本可选）：openai-whisper
          - 系统运行时：ffmpeg（必需）、deno（Dockerfile 默认安装，作 yt‑dlp JS runtime）；可选 node/bun/quickjs
          - 前端：Bootstrap 5.3 + Bootstrap Icons + Socket.IO 4.7（均走 CDN，写在 index.html）

• 运行时模型(yt_downloader.DownloadManager)— 方案 2 + 后处理并行

  - 设计目标
      - 网络下载阶段串行,独占代理出口,降低 SSL/限速抖动
      - 后处理(ffmpeg merge / EmbedThumbnail / FFmpegMetadata 等)与下一条任务的网络下载并行,
        让带宽和 CPU 都不闲置
      - 任务按入队 FIFO 顺序进入下载,可见、可取消、可幂等释放槽位

  - 启动
      - 按 THREAD_COUNT 拉起守护 worker 线程,共享同一 download_queue;
        网络下载并发恒为 1,worker 池实际服务于"等待网络槽位 + 后处理并行"

  - 全局公平网络槽位(_acquire_net_slot / _release_net_slot)
      - 用 threading.Condition + collections.deque 实现 FIFO 票据队列,
        替代普通 Semaphore;避免多 worker 轮询时被 OS 调度乱序
      - 等待者按 download_id 入队;只有"队首 + 锁空闲"才能拿到槽位
      - wait 带 0.5s 超时,以便定期检查 stop_event;异常路径会从队列移除并 notify_all
      - 释放采用幂等闭包 _release_network_lock(reason),记录释放原因便于诊断

  - 单条任务流程(_download_item)
      1. 入 Waiting:item.status="Waiting"、progress="Queued",emit update_download_item
      2. 调 _acquire_net_slot:被取消立即返回(标 Cancelled,不占槽位)
      3. 入 In Progress:status="In Progress"、progress="0%"、清 _first_progress_emitted,emit
      4. 构建 ydl_opts(含 progress_hooks 与 postprocessor_hooks,均闭包持有 _release_network_lock)
      5. 调 ydl.download:
         - progress_hook downloading:首次强制 emit、之后 1/10 抽样;实时刷新 percent/speed
         - progress_hook finished:仅刷新 UI("Downloaded"/"Processing"),并置位 _streams_completed
         - postprocessor_hook started:**当且仅当 _streams_completed=True 才释放网络槽位**;
           UI 同步显示 "Processing (<PostProcessor>)",before_dl 阶段不显示也不释放
      6. finally:_release_network_lock("finally") 兜底幂等释放、emit 终态、ydl.close()

  - 关键不变量(踩过的坑)
      - 释放网络槽位的真实信号是 progress_hook.finished,不是 postprocessor_hook.started
        (后者会被 before_dl 的 FFmpegThumbnailsConvertor / FFmpegSubtitlesConvertor / TrimDescriptionPP 误触发,
         如果直接释放会让"串行"破功,导致多条同时下载)
      - 释放点最终落在"_streams_completed 置位后,首个 PostProcessor started"——
        即真正的 Merger / EmbedThumbnail 等开始时;此时网络确认空闲,可以让下一条进入
      - 必须 FIFO 公平:Semaphore 不公平,多 worker 抢锁会乱序;改用 Condition + deque 强制顺序
      - In Progress 阶段必须显式重置 progress 字段,否则前端会保留 "Queued" 文案
      - 首次 downloading 进度回调必须强制 emit,绕过 1/10 降频,避免前端长时间停在 "0%"

  - 状态语义(前端表格 status 列)
      - Pending:已入队,worker 还没取走
      - Waiting:worker 已取走,正在 FIFO 排队等网络槽位
      - In Progress:已拿到槽位,准备开始下载
      - Downloading:downloading 阶段进度回调中
      - Processing / Processing (<PostProcessor>):后处理阶段(网络槽位已让出)
      - Complete / Cancelled / Failed: <ExceptionName>

  - 取消机制
      - 每条 item 一把 stop_signals[id] = threading.Event
      - Waiting 阶段:_acquire_net_slot 每 0.5s 检查 stop_event,取消立即退出(不占槽位)
      - Downloading 阶段:_progress_hook 检测 is_set() → 抛 DownloadCancelledException
      - cancel_items:set Event + 状态改 Cancelling
      - remove_items:set Event + 从 all_items / stop_signals 删除 + emit remove_download_item

  - 进度上报与日志
      - downloading 状态:首次强制 emit、之后 random.randint(1,10)!=1 抽样 emit
      - 直播流上报 "Frag: <index> (<elapsed>)";普通流上报 "<percent> at <speed>"
      - 首次进入"真实视频流"打印一次 id/note/widthxheight/fps/ext/vcodec/acodec
      - 释放网络槽位时打印 "[net-slot] released by <reason> for download_id=<id>",可作诊断锚点

  - 后处理器组合(默认)
      - SponsorBlock(sponsor) → ModifyChapters(remove sponsor) → FFmpegThumbnailsConvertor(png, before_dl) → EmbedThumbnail → FFmpegMetadata
      - 仅音频追加:FFmpegExtractAudio(preferredcodec=audio_ext, q=0)
      - 字幕:WRITE_SUBS=true 追加 FFmpegSubtitlesConvertor(format=SUBTITLE_FORMAT, before_dl);
        EMBED_SUBS=true 追加 FFmpegEmbedSubtitle(already_have_subtitle=WRITE_SUBS)
      - TRIM_METADATA=true:ydl.add_post_processor(helpers.TrimDescriptionPP(), when="before_dl")

  - 选轨
      - format = "{video_format_id}+{audio_format_id}/bestvideo+bestaudio/best"
        (仅音频时为 "{audio_format_id}/bestaudio/best")
      - format_sort = [lang:<PREFERRED_LANGUAGE>, acodec:<…>, quality, size, vcodec:<…>, vext:<…>]
      - 非音频任务还会设 merge_output_format=mp4

  - 网络鲁棒性(_download_item 的 ydl_opts 内置)
      - retries=30 / fragment_retries=30 / extractor_retries=5 / file_access_retries=5
      - retry_sleep_functions:http/fragment/extractor 均指数退避封顶 30s
      - socket_timeout=30、http_chunk_size=10MiB、continuedl=True

  - 临时目录与缓存
      - temp_folder = TUBETUBE_TEMP_DIR or ~/.tubetube/temp
        (启动即创建并清理 .tmp/.part/.webp/.ytdl/.png/.<sub_format>)
      - cachedir = <temp>/cache(给 ydl_for_parsing)

  - JS runtime
      - 优先解析 JS_RUNTIMES 配置(支持 "name" 或 "name:/path" 逗号分隔,亦支持 dict)
      - 未配置时自动检测 deno/node/bun/quickjs(qjs);找到的以 {path: …} 注入 ydl_opts["js_runtimes"]

  - THREAD_COUNT 语义说明
      - 名义上是"worker 线程数",但因网络下载强制串行,**它实质上限制的是"同时进行后处理的并发上限"**
      - 取值建议:2~4。过小会造成"上一条还没合并完,网络槽位空着没人接"的浪费;
        过大对一台机器的 CPU/磁盘 IO 收益递减,反而拖慢正在下载的那条的写入

• Socket.IO 事件契约（前后端约定）

  - 客户端 → 服务器：connect、download、remove_items、cancel_items
  - 服务器 → 客户端：update_folder_locations、update_download_list、update_download_item、remove_download_item、toast

• 配置默认值差异（务必区分）

  - 代码默认（yt_downloader.DEFAULT_APP_CONFIG）偏通用：PREFERRED_LANGUAGE=zh-Hant、SUBTITLE_LANGUAGES=zh-Hant、WRITE_SUBS=True、ALLOW_AUTO_SUBS=True、THREAD_COUNT=4，其余编码偏 aac/vp9/mp4
  - 仓库 config/app_config.yaml（运行时实际读取的样例）：
      - JS_RUNTIMES: "node"
      - PREFERRED_LANGUAGE: zh-Hant
      - SUBTITLE_LANGUAGES: zh-Hant,zh-Hans,zh
      - WRITE_SUBS: true、ALLOW_AUTO_SUBS: true、EMBED_SUBS: false
  - 同名环境变量优先级最高，可逐项覆盖 app_config.yaml 与代码默认

• 注意事项与已知坑

  - 容器内 /temp 需要显式设置 TUBETUBE_TEMP_DIR=/temp 才会被使用
      - Dockerfile/start.sh 会创建并 chown /temp，但 yt_downloader 仅认 TUBETUBE_TEMP_DIR；未设置时会写到容器内 ~/.tubetube/temp，宿主挂载 /temp 不会生效
  - 本地直跑需 PYTHONPATH=tubetube（或先 cd tubetube），否则 from settings/yt_downloader 导入会失败
  - cookie 行为：一个视频通常需要一份独立 cookie；遇 “SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC” 一般重试或更换 cookie 即可（详见 README）
  - yt‑dlp 对 Node 的最低支持是 v20；Dockerfile 默认安装的是 deno，如需用 node 请自行加装
  - 串行下载诊断锚点
      - 正确串行的特征:任意时刻日志里只有一条 "[download] x.x% of ..." 在刷;
        每条任务在结束后会出现 "[net-slot] released by postprocessor:Merger for download_id=N",
        紧随下一条 "Starting Worker-? Download: ..." 才出现下一条 "Download video format: ..."
      - 异常征兆:
          1) 多条任务同时刷 "[download] x.x% of ..." → before_dl 阶段误释放,检查
             postprocessor_hook 必须在 _streams_completed=True 后才调用 release
          2) 释放原因显示 "ThumbnailsConvertor"/"SubtitlesConvertor" → 同上,门闸失效
          3) FIFO 顺序错乱(后入队的先开始下) → 网络槽位换回 Semaphore 了,需用
             Condition + deque 的公平实现
          4) 前端 progress 长时间停在 "Queued" → 进入 In Progress 时没重置 progress,
             或首次 downloading 回调被 1/10 抽样吞掉,需强制首次 emit

• 常用本地命令

  - 启动后端（开发）：PYTHONPATH=tubetube python tubetube/tubetube.py
  - 跑 VTT 处理脚本：python scripts/process_vtt.py
  - 跑 Whisper 转写脚本：python scripts/transcribe_mp4_with_whisper.py --input data/Video/xxx.mp4
  - 构建镜像：docker build -t tubetube .
