import re
import os
import queue
import logging
import platform
import threading
import random
import shutil
import collections
import yaml
import yt_dlp
from settings import DownloadCancelledException
import helpers


DEFAULT_APP_CONFIG = {
    "VERBOSE_LOGS": False,
    "TRIM_METADATA": False,
    "PROXY": "",
    "JS_RUNTIMES": "",
    "PREFERRED_LANGUAGE": "zh-Hant",
    "PREFERRED_AUDIO_CODEC": "aac",
    "PREFERRED_VIDEO_CODEC": "vp9",
    "PREFERRED_VIDEO_EXT": "mp4",
    "EMBED_SUBS": False,
    "WRITE_SUBS": True,
    "ALLOW_AUTO_SUBS": True,
    "SUBTITLE_FORMAT": "vtt",
    "SUBTITLE_LANGUAGES": "zh-Hant",
    "THREAD_COUNT": 16,
}


class DownloadManager:
    def __init__(self):
        self.download_queue = queue.Queue()
        self.all_items = {}
        self.lock = threading.Lock()
        self.stop_signals = {}
        # 全局网络下载并发控制:同一时间只允许 1 条任务进入"网络下载"阶段。
        # 后处理(ffmpeg merge / embed / metadata 等)不受此锁限制,由 worker 池天然并发。
        # 用 FIFO 票据队列 + Condition 实现"按 download_id 入队顺序"的公平锁,
        # 避免普通 Semaphore 在多 worker 轮询时被 OS 调度打乱顺序。
        self._net_cv = threading.Condition()
        self._net_busy = False
        self._net_waiters = collections.deque()  # FIFO 票据,元素为 download_id

        os_system = platform.system()
        logging.info(f"OS: {os_system}")

        self.ffmpeg_location = self._resolve_ffmpeg_path(os_system)
        logging.info(f"FFmpeg location set to: {self.ffmpeg_location}")

        self.app_config_path = self._resolve_app_config_path()
        self.app_config = self._load_app_config(self.app_config_path)

        self.verbose_ytdlp = self._get_bool("VERBOSE_LOGS", False)
        logging.info(f"Verbose logging for yt-dlp set to: {self.verbose_ytdlp}")

        self.trim_metadata = self._get_bool("TRIM_METADATA", False)
        logging.info(f"Trim Metadata set to: {self.trim_metadata}")

        proxy_value = self._get_str("PROXY", "")
        self.proxy = proxy_value.strip() if isinstance(proxy_value, str) else ""
        self.proxy = self.proxy or None
        if self.proxy:
            logging.info("Proxy enabled for yt-dlp requests.")
        else:
            logging.info("Proxy disabled for yt-dlp requests.")

        self.js_runtimes = self._parse_js_runtimes(self._get_config_value("JS_RUNTIMES", ""))
        if not self.js_runtimes:
            self.js_runtimes = self._auto_detect_js_runtimes()
        if self.js_runtimes:
            logging.info(f"JS runtimes for yt-dlp: {self.js_runtimes}")
        else:
            logging.info("No JS runtime configured for yt-dlp.")

        self.preferred_language = self._get_str("PREFERRED_LANGUAGE", "en")
        logging.info(f"Preferred Audio Language: {self.preferred_language}")

        self.preferred_audio_codec = self._get_str("PREFERRED_AUDIO_CODEC", "aac")
        logging.info(f"Preferred Audio Codec: {self.preferred_audio_codec}")

        self.preferred_video_codec = self._get_str("PREFERRED_VIDEO_CODEC", "vp9")
        logging.info(f"Preferred Video Codec: {self.preferred_video_codec}")

        self.preferred_video_ext = self._get_str("PREFERRED_VIDEO_EXT", "mp4")
        logging.info(f"Preferred Video Ext: {self.preferred_video_ext}")

        self.embed_subs = self._get_bool("EMBED_SUBS", False)
        logging.info(f"Embed Subtitles: {self.embed_subs}")

        self.write_subs = self._get_bool("WRITE_SUBS", False)
        logging.info(f"Write Subtitles: {self.write_subs}")

        self.allow_auto_subs = self._get_bool("ALLOW_AUTO_SUBS", True)
        logging.info(f"Automatic Subtitles Enabled: {self.allow_auto_subs}")

        self.subtitle_format = self._get_str("SUBTITLE_FORMAT", "vtt")
        logging.info(f"Subtitle Format: {self.subtitle_format}")

        subtitle_langs_raw = self._get_config_value("SUBTITLE_LANGUAGES", "en")
        self.subtitle_languages = self._parse_languages(subtitle_langs_raw)
        logging.info(f"Subtitle Languages: {self.subtitle_languages}")

        self.subtitle_config = {
            "subtitlesformat": "best",
            "subtitleslangs": self.subtitle_languages,
            "writeautomaticsub": self.allow_auto_subs,
            "writesubtitles": self.write_subs,
        }
        self.subtitle_pps = []
        if self.write_subs:
            self.subtitle_pps.append({"key": "FFmpegSubtitlesConvertor", "format": self.subtitle_format, "when": "before_dl"})
        if self.embed_subs:
            self.subtitle_pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": self.write_subs})

        self.thread_count = self._get_int("THREAD_COUNT", 16)
        logging.info(f"Thread Count: {self.thread_count}")

        for i in range(self.thread_count):
            worker = threading.Thread(target=self._process_queue, daemon=True, name=f"Worker-{i}")
            worker.start()
            logging.info(f"Started thread: {worker.name}")

        temp_env = os.getenv("TUBETUBE_TEMP_DIR")
        self.temp_folder = temp_env if temp_env else os.path.expanduser("~/.tubetube/temp")
        os.makedirs(self.temp_folder, exist_ok=True)

        parsing_opts = {
            "quiet": True,
            "no_color": True,
            "extract_flat": True,
            "ignore_no_formats_error": True,
            "force_generic_extractor": False,
            "cachedir": os.path.join(self.temp_folder, "cache"),
            "noprogress": True,
            "no_warnings": True,
        }
        if self.proxy:
            parsing_opts["proxy"] = self.proxy
        if self.js_runtimes:
            parsing_opts["js_runtimes"] = self.js_runtimes
        self.ydl_for_parsing = yt_dlp.YoutubeDL(parsing_opts)

        self.cleanup_temp_folder()

    def cleanup_temp_folder(self):
        try:
            removable_extensions = (".tmp", ".part", ".webp", ".ytdl", ".png", f".{self.subtitle_format}")
            for file_name in os.listdir(self.temp_folder):
                file_path = os.path.join(self.temp_folder, file_name)
                if os.path.isfile(file_path) and file_name.endswith(removable_extensions):
                    os.remove(file_path)
                    logging.info(f"Deleted file: {file_path}")

        except Exception as e:
            logging.error(f"Error cleaning up temporary folder: {e}")

    def _resolve_ffmpeg_path(self, os_system):
        path = shutil.which("ffmpeg")
        if path:
            return path

        if os_system == "Windows":
            candidates = [
                r"C:\ffmpeg\bin\ffmpeg.exe",
                r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
                r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
                r"D:\ffmpeg\bin\ffmpeg.exe",
            ]
        elif os_system == "Darwin":
            candidates = [
                "/opt/homebrew/bin/ffmpeg",
                "/usr/local/bin/ffmpeg",
                "/usr/bin/ffmpeg",
            ]
        else:
            candidates = [
                "/usr/bin/ffmpeg",
                "/usr/local/bin/ffmpeg",
            ]

        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate

        return "ffmpeg"

    def _resolve_app_config_path(self):
        config_path = os.getenv("TUBETUBE_APP_CONFIG")
        if config_path:
            return config_path
        config_folder = getattr(self, "config_folder", None)
        if config_folder:
            return os.path.join(config_folder, "app_config.yaml")
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        return os.path.join(repo_root, "config", "app_config.yaml")

    def _load_app_config(self, config_path):
        if not config_path:
            return DEFAULT_APP_CONFIG.copy()
        config_dir = os.path.dirname(config_path)
        if config_dir:
            os.makedirs(config_dir, exist_ok=True)
        if not os.path.exists(config_path):
            try:
                with open(config_path, "w") as file:
                    yaml.safe_dump(DEFAULT_APP_CONFIG, file, default_flow_style=False)
            except OSError as e:
                logging.error(f"Unable to write app config: {e}")
            return DEFAULT_APP_CONFIG.copy()

        try:
            with open(config_path, "r") as file:
                return yaml.safe_load(file) or DEFAULT_APP_CONFIG.copy()
        except (OSError, yaml.YAMLError) as e:
            logging.error(f"App config loading error: {e}")
            return DEFAULT_APP_CONFIG.copy()

    def _get_config_value(self, env_key, default):
        env_value = os.getenv(env_key)
        if env_value is not None:
            return env_value
        if not isinstance(self.app_config, dict):
            return default
        if env_key in self.app_config:
            return self.app_config.get(env_key)
        lower_key = env_key.lower()
        if lower_key in self.app_config:
            return self.app_config.get(lower_key)
        return default

    def _parse_bool(self, value, default=False):
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        value_str = str(value).strip().lower()
        if not value_str:
            return default
        if value_str in {"1", "true", "yes", "on"}:
            return True
        if value_str in {"0", "false", "no", "off"}:
            return False
        return default

    def _get_bool(self, env_key, default):
        return self._parse_bool(self._get_config_value(env_key, default), default)

    def _get_int(self, env_key, default):
        raw_value = self._get_config_value(env_key, default)
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return default

    def _get_str(self, env_key, default):
        raw_value = self._get_config_value(env_key, default)
        if raw_value is None:
            return default
        return str(raw_value)

    def _parse_languages(self, raw_languages):
        if isinstance(raw_languages, (list, tuple, set)):
            values = raw_languages
        else:
            values = str(raw_languages or "").split(",")
        languages = [str(lang).strip() for lang in values if str(lang).strip()]
        return languages if languages else ["en"]

    def _parse_js_runtimes(self, raw_runtimes):
        if raw_runtimes is None:
            return {}
        if isinstance(raw_runtimes, dict):
            normalized = {}
            for runtime, config in raw_runtimes.items():
                runtime_name = str(runtime).strip().lower()
                if not runtime_name:
                    continue
                normalized[runtime_name] = config if isinstance(config, dict) else {}
            return normalized
        if isinstance(raw_runtimes, (list, tuple, set)):
            values = raw_runtimes
        else:
            values = str(raw_runtimes or "").split(",")
        runtimes = {}
        for value in values:
            value_str = str(value).strip()
            if not value_str:
                continue
            runtime, path = (value_str.split(":", 1) + [None])[:2]
            runtime_name = runtime.strip().lower()
            if not runtime_name:
                continue
            config = {}
            if path:
                path = path.strip()
                if path:
                    config["path"] = path
            runtimes[runtime_name] = config
        return runtimes

    def _auto_detect_js_runtimes(self):
        candidates = {
            "deno": "deno",
            "node": "node",
            "bun": "bun",
            "quickjs": "qjs",
        }
        runtimes = {}
        for runtime, binary in candidates.items():
            path = shutil.which(binary)
            if path:
                runtimes[runtime] = {"path": path}
        return runtimes

    def add_to_queue(self, item_info):
        url = item_info.get("url", "")
        logging.info(f"Processing URL: {url}")

        if "&list=" in url:
            url = re.sub(r"&list=.*", "", url)

        with self.lock:
            parsed_identifier = helpers.parse_video_id(url)
            if any(item["url"] == url or item["video_identifier"] == parsed_identifier for item in self.all_items.values()):
                logging.info(f"URL {url} is already in the queue or being downloaded.")
                self.socketio.emit("toast", {"title": "Duplicate URL", "body": f"The video '{url}' is already in the queue or being processed."})
                return

        try:
            yt_info_dict = self.ydl_for_parsing.extract_info(url, download=False)
            logging.info(f"Extracted info for {yt_info_dict.get('title', 'unknown')}")

        except Exception as e:
            logging.error(f"Error extracting info: {e}")
            logging.error(f"Nothing Added to Queue")
            self.socketio.emit("toast", {"title": "Failed to add item to the queue.", "body": f"Please check the URL.\n\n {str(e)}"})
            return

        if "entries" in yt_info_dict:
            playlist_name = re.sub(r'[<>:"/\\|?*]', "-", yt_info_dict.get("title"))
            item_info["folder_name"] = f'{item_info.get("folder_name")}/{playlist_name}'
            logging.info(f"Adding playlist: {playlist_name} to queue")
            for entry in yt_info_dict["entries"]:
                self._enqueue_item(entry, item_info)
        else:
            self._enqueue_item(yt_info_dict, item_info)

    def _enqueue_item(self, yt_info_dict, item_info):
        try:
            download_id = max(self.all_items.keys(), default=-1) + 1
            url = yt_info_dict.get("webpage_url", yt_info_dict.get("url"))
            item = {
                "video_identifier": yt_info_dict.get("id"),
                "id": download_id,
                "title": yt_info_dict.get("title"),
                "url": url,
                # 初始状态直接标 Waiting,与 worker 接管后等网络槽位的状态一致;
                # 这样前端看到的"还没开始下载"统一为 Waiting,避免出现短暂的 Pending → Waiting 切换。
                "status": "Waiting",
                "progress": "Queued",
                "folder_name": item_info.get("folder_name"),
                "download_settings": item_info.get("download_settings"),
                "audio_only": item_info.get("audio_only"),
                "skipped": False,
            }
            with self.lock:
                self.all_items[download_id] = item
                self.stop_signals[download_id] = threading.Event()
            self.download_queue.put(download_id)
            logging.info(f'Queued item: {item["title"]} with ID: {download_id}')
            self.socketio.emit("update_download_list", self.all_items)

        except Exception as e:
            logging.error(f"Error enqueuing item: {e}")
            logging.warning(f'Failed to add: {yt_info_dict.get("title")} to the queue.')

        else:
            logging.info(f'Added: {yt_info_dict.get("title")} to queue.')

    def _process_queue(self):
        while True:
            try:
                download_id = self.download_queue.get()
                logging.info(f"Processing download ID: {download_id} in thread {threading.current_thread().name}")

                if self.all_items[download_id]["skipped"]:
                    self.all_items[download_id]["status"] = "Cancelled"
                    logging.info(f"Item {download_id} marked as skipped.")
                    self.socketio.emit("update_download_item", {"item": self.all_items[download_id]})

                else:
                    self._download_item(download_id)

            except Exception as e:
                logging.error(f"Processing error for ID {download_id}: {e}")

            finally:
                self.download_queue.task_done()
                if self.download_queue.empty():
                    logging.info(f"Queue is empty.")

    def _acquire_net_slot(self, download_id, stop_event):
        """按 FIFO 顺序公平获取网络下载槽位;期间响应 cancel。返回 True=拿到槽位,False=被取消。"""
        with self._net_cv:
            self._net_waiters.append(download_id)
        acquired = False
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return False
                with self._net_cv:
                    # 队首并且锁空闲时才能拿到槽位
                    if not self._net_busy and self._net_waiters and self._net_waiters[0] == download_id:
                        self._net_busy = True
                        self._net_waiters.popleft()
                        acquired = True
                        return True
                    # 否则等通知,带超时以便定期检查 cancel
                    self._net_cv.wait(timeout=0.5)
        finally:
            # 任何未拿到槽位的退出路径(被取消、异常)都必须把自己从等待队列里清掉,
            # 否则该 id 会永远卡在队首,阻塞后续所有新任务进入下载。
            if not acquired:
                with self._net_cv:
                    try:
                        self._net_waiters.remove(download_id)
                    except ValueError:
                        pass
                    self._net_cv.notify_all()

    def _release_net_slot(self):
        with self._net_cv:
            if self._net_busy:
                self._net_busy = False
                self._net_cv.notify_all()

    def _download_item(self, download_id):
        item = self.all_items[download_id]

        # ---- 阶段 1:等待网络下载槽位(全局只允许 1 路,FIFO 公平) ----
        item["status"] = "Waiting"
        item["progress"] = "Queued"
        self.socketio.emit("update_download_item", {"item": item})

        stop_event = self.stop_signals.get(download_id)
        if not self._acquire_net_slot(download_id, stop_event):
            item["status"] = "Cancelled"
            item["progress"] = "Cancelled"
            self.socketio.emit("update_download_item", {"item": item})
            return

        network_lock_released = {"value": False}

        def _release_network_lock(reason):
            # 幂等释放,可被 progress_hook / postprocessor_hook / finally 任意一处触发
            if network_lock_released["value"]:
                return
            network_lock_released["value"] = True
            self._release_net_slot()
            logging.info(f"[net-slot] released by {reason} for download_id={download_id}")

        # ---- 阶段 2:进入下载 ----
        item["status"] = "In Progress"
        # 重置 progress,避免前端继续显示等待时设置的 "Queued"。
        # 同时清掉首次进度上报的标记,确保第一次 downloading 回调一定会 emit。
        item["progress"] = "0%"
        item.pop("_first_progress_emitted", None)
        self.socketio.emit("update_download_item", {"item": item})

        download_settings = item.get("download_settings")
        folder_name = item.get("folder_name")

        video_format_id = download_settings.get("video_format_id", {})
        audio_format_id = download_settings.get("audio_format_id", {})

        if item.get("audio_only"):
            download_format = f"{audio_format_id}/bestaudio/best"
        else:
            download_format = f"{video_format_id}+{audio_format_id}/bestvideo+bestaudio/best"

        item_title = re.sub(r'[<>:"/\\|?*]', "-", item.get("title"))
        final_path = os.path.join(getattr(self, "data_folder", "/data"), folder_name)

        ydl_opts = {
            "ignore_no_formats_error": True,
            "noplaylist": True,
            "outtmpl": f"{item_title}.%(ext)s",
            "progress_hooks": [lambda d: self._progress_hook(d, download_id, _release_network_lock)],
            "postprocessor_hooks": [lambda d: self._postprocessor_hook(d, download_id, _release_network_lock)],
            "ffmpeg_location": self.ffmpeg_location,
            "writethumbnail": True,
            "quiet": not self.verbose_ytdlp,
            "extract_flat": True,
            "format": download_format,
            "updatetime": False,
            "live_from_start": True,
            "extractor_args": {"youtubetab": {"skip": ["authcheck"]}},
            "paths": {"home": final_path, "temp": self.temp_folder},
            "no_overwrites": True,
            "verbose": self.verbose_ytdlp,
            "no_mtime": True,
            # 网络抖动 / 代理 / SSL 偶发错误下的鲁棒性配置
            "retries": 30,                  # 整段下载的最大重试
            "fragment_retries": 30,         # 分片下载的最大重试(直播/HLS/DASH)
            "extractor_retries": 5,         # 解析阶段的重试
            "file_access_retries": 5,
            "retry_sleep_functions": {      # 指数退避,封顶 30s
                "http": lambda n: min(2 ** n, 30),
                "fragment": lambda n: min(2 ** n, 30),
                "extractor": lambda n: min(2 ** n, 30),
            },
            "socket_timeout": 30,           # 单次 socket 操作超时
            "http_chunk_size": 10 * 1024 * 1024,  # 10MiB 分块,降低长连接被中断的概率
            "continuedl": True,             # 断点续传
            "format_sort": [f"lang:{self.preferred_language}", f"acodec:{self.preferred_audio_codec}", "quality", "size", f"vcodec:{self.preferred_video_codec}", f"vext:{self.preferred_video_ext}"],
        }
        if self.proxy:
            ydl_opts["proxy"] = self.proxy
        if self.js_runtimes:
            ydl_opts["js_runtimes"] = self.js_runtimes

        post_processors = [
            {"key": "SponsorBlock", "categories": ["sponsor"]},
            {"key": "ModifyChapters", "remove_sponsor_segments": ["sponsor"]},
        ]

        if item.get("audio_only"):
            audio_ext = download_settings.get("audio_ext", "m4a")
            post_processors.extend([{"key": "FFmpegExtractAudio", "preferredcodec": audio_ext, "preferredquality": "0"}])

        post_processors.append({"key": "FFmpegThumbnailsConvertor", "format": "png", "when": "before_dl"})
        post_processors.append({"key": "EmbedThumbnail"})
        post_processors.append({"key": "FFmpegMetadata"})

        if not item.get("audio_only"):
            ydl_opts["merge_output_format"] = "mp4"

        if self.cookies_file:
            ydl_opts["cookiefile"] = self.cookies_file

        if self.write_subs or self.embed_subs:
            ydl_opts.update(self.subtitle_config)
            post_processors.extend(self.subtitle_pps)

        ydl_opts["postprocessors"] = post_processors

        ydl = None
        try:
            logging.info(f'Starting {threading.current_thread().name} Download: {item.get("title")}')
            ydl = yt_dlp.YoutubeDL(ydl_opts)
            if self.trim_metadata:
                ydl.add_post_processor(helpers.TrimDescriptionPP(), when="before_dl")
            result = ydl.download([item["url"]])
            item["progress"] = "Done" if result == 0 else "Incomplete"
            item["status"] = "Complete"
            logging.info(f'Finished {threading.current_thread().name} Download: {item.get("title")}')

        except DownloadCancelledException:
            item["status"] = "Cancelled"
            logging.info(f'Download cancelled: {item.get("title")}')

        except Exception as e:
            item["status"] = f"Failed: {type(e).__name__}"
            item["progress"] = "Error"
            logging.error(f'Error downloading: {item.get("title")} - {str(e)}')

        finally:
            # 兜底:即便 progress_hook / postprocessor_hook 都没触发(比如直接异常退出),也不能泄漏锁。
            _release_network_lock("finally")
            self.socketio.emit("update_download_item", {"item": item})
            if ydl is not None:
                ydl.close()

    def _progress_hook(self, d, download_id, release_network_lock=None):
        if self.stop_signals[download_id].is_set():
            raise DownloadCancelledException("Cancelled")

        if d["status"] == "downloading":
            with self.lock:
                item = self.all_items[download_id]
                self._log_video_format_if_needed(item, d)

                # 首次进度回调强制 emit,绕过 1/10 降频。
                # 避免前端 progress 列长时间停留在 "Queued"/"0%"。
                first_emit = not item.get("_first_progress_emitted")
                if not first_emit and random.randint(1, 10) != 1:
                    return
                item["_first_progress_emitted"] = True

                live = d.get("info_dict", {}).get("is_live", False)
                if live:
                    fragment_index_str = d.get("fragment_index", 1)
                    elapsed_str = re.sub(r"\x1b\[[0-9;]*m", "", d.get("_elapsed_str", "")).strip()
                    progress_message = f"Frag: {fragment_index_str} ({elapsed_str})"
                else:
                    percent_str = re.sub(r"\x1b\[[0-9;]*m", "", d.get("_percent_str", "")).strip()
                    speed_str = re.sub(r"\x1b\[[0-9;]*m", "", d.get("_speed_str", "")).strip()
                    progress_message = f"{percent_str} at {speed_str}"

                item["progress"] = progress_message
                item["status"] = "Downloading"
                self.socketio.emit("update_download_item", {"item": item})

        elif d["status"] == "finished":
            with self.lock:
                item = self.all_items[download_id]
                item["progress"] = "Downloaded"
                item["status"] = "Processing"
                # 标记"真实媒体流已下载完成":progress_hook 的 finished 只对实际 format 流触发,
                # 不会对 before_dl 的 postprocessor(如 FFmpegThumbnailsConvertor)触发。
                # 这是判断"网络阶段是否结束"的可靠信号,网络锁的释放必须等这个标记置位。
                item["_streams_completed"] = True
                logging.info(f'Download finished: {item.get("title")} - processing now')
                self.socketio.emit("update_download_item", {"item": item})

    def _postprocessor_hook(self, d, download_id, release_network_lock=None):
        # 关键:before_dl 的后处理器(FFmpegThumbnailsConvertor / FFmpegSubtitlesConvertor / TrimDescriptionPP)
        # 在真正下载开始之前就会触发 started,如果在这里直接释放锁会让"串行"破功。
        # 用 _streams_completed 作为门闸:只有 progress_hook 的 finished 触发过,才意味着
        # 网络下载真的结束了,此时第一个 after_dl 的 PP started 即可释放槽位让下一条进入下载。
        status = d.get("status")
        pp_name = d.get("postprocessor") or "PostProcessing"

        if status == "started" and release_network_lock is not None:
            with self.lock:
                item = self.all_items.get(download_id)
                streams_done = bool(item and item.get("_streams_completed"))
            if streams_done:
                release_network_lock(f"postprocessor:{pp_name}")

        # 同步前端:让用户看到"处理中:xxx";同样用 _streams_completed 过滤掉 before_dl 阶段
        if status in ("started", "processing"):
            with self.lock:
                item = self.all_items.get(download_id)
                if not item:
                    return
                if not item.get("_streams_completed"):
                    return
                item["status"] = f"Processing ({pp_name})"
                self.socketio.emit("update_download_item", {"item": item})

    def _log_video_format_if_needed(self, item, d):
        if item.get("video_format_logged"):
            return

        info = d.get("info_dict") or {}
        height = info.get("height")
        width = info.get("width")
        vcodec = info.get("vcodec")

        is_video_stream = bool(height or (vcodec and vcodec != "none"))
        if not is_video_stream:
            return

        parts = []
        format_id = info.get("format_id")
        if format_id:
            parts.append(f"id={format_id}")
        format_note = info.get("format_note")
        if format_note:
            parts.append(f"note={format_note}")
        if width and height:
            parts.append(f"{width}x{height}")
        elif height:
            parts.append(f"{height}p")
        fps = info.get("fps")
        if fps:
            parts.append(f"{fps}fps")
        ext = info.get("ext")
        if ext:
            parts.append(ext)
        if vcodec and vcodec != "none":
            parts.append(f"vcodec={vcodec}")
        acodec = info.get("acodec")
        if acodec and acodec != "none":
            parts.append(f"acodec={acodec}")

        summary = " ".join(parts) if parts else "unknown format"
        logging.info(f'Download video format: {summary} | title="{item.get("title")}"')
        item["video_format_logged"] = True

    def cancel_items(self, item_ids):
        with self.lock:
            for item_id in item_ids:
                if item_id in self.all_items:
                    self.all_items[item_id]["skipped"] = True
                    self.all_items[item_id]["status"] = "Cancelling"
                    logging.info(f"Item {item_id} marked for cancellation.")
                    if item_id in self.stop_signals:
                        self.stop_signals[item_id].set()
                    self.socketio.emit("update_download_item", {"item": self.all_items[item_id]})

    def remove_items(self, item_ids):
        with self.lock:
            for item_id in item_ids:
                if item_id in self.all_items:
                    logging.info(f"Removing item {item_id}")
                    if item_id in self.stop_signals:
                        self.stop_signals[item_id].set()
                    del self.all_items[item_id]
                    if item_id in self.stop_signals:
                        del self.stop_signals[item_id]
                    self.socketio.emit("remove_download_item", {"id": item_id})

        # 兜底清扫:被删除的 id 不应再留在网络槽位等待队列里;
        # 同时 notify_all 唤醒可能正在等待这些 id 的 worker,让它们立刻退出。
        with self._net_cv:
            removed = False
            for item_id in item_ids:
                while True:
                    try:
                        self._net_waiters.remove(item_id)
                        removed = True
                    except ValueError:
                        break
            if removed:
                self._net_cv.notify_all()
