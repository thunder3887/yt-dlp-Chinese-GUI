import tkinter as tk
from tkinter import ttk, messagebox
import threading
import os
import re
from urllib.parse import urlparse, parse_qs
import yt_dlp
from yt_dlp import utils as ydl_utils
import shutil, subprocess
import sys, signal

SITE_SPECIFIC_OPTS = {
    "twitcasting.tv": {"live_from_start": True},

    "youtube.com": {
        "writesubtitles": True,
        "subtitleslangs": ["zh-Hant", "zh-TW", "zh"],
        "ignoreerrors": True,
        "writeautomaticsub": False,
    },
    "youtu.be": "youtube.com",

    "bilibili.com": {"http_headers": {"Referer": "https://www.bilibili.com/"}},

    "twitch.tv": {
        "live_from_start": True,
    },

    "twitter.com": {"extractor_args": {"twitter": {"api": ["syndication"]}}},
    "x.com": "twitter.com",
}

def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if 'youtu.be' in url:
        vid = urlparse(url).path.lstrip('/')
        q = urlparse(url).query
        return f'https://www.youtube.com/watch?v={vid}' + (f'&{q}' if q else '')
    if '/shorts/' in url:
        vid = url.split('/shorts/')[-1].split('?')[0]
        q = urlparse(url).query
        return f'https://www.youtube.com/watch?v={vid}' + (f'&{q}' if q else '')
    parsed = urlparse(url)
    if parsed.netloc.endswith('youtube.com') and parsed.path == '/watch':
        qs = parse_qs(parsed.query)
        if 'v' in qs:
            rest = '&'.join(f'{k}={v[0]}' for k,v in qs.items() if k != 'v')
            return f"https://www.youtube.com/watch?v={qs['v'][0]}" + (f"&{rest}" if rest else '')
    return url

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')   

def strip_ansi(txt: str) -> str:
    """
    去掉 yt‑dlp 內嵌的 ANSI 顏色碼。
    """
    return ANSI_RE.sub('', txt)

class YTDownloaderGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video Downloader")
        self.geometry("560x320")
        self.resizable(False, False)
        self.paused = False              
        self.pause_flag = False          
        self.current_url = None          
        self.stop_flag = False           
        self.last_filename = None        
        self.source_bitrate_kbps = None  
        self.ffprobe_warned = False      
        self.live_proc = None

        ttk.Label(self, text="影片 / 播放清單 URL:").pack(pady=(20, 5), anchor="w", padx=20)
        self.url_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.url_var, width=75).pack(padx=20)

        self.format_var = tk.StringVar(value="mp4")
        fmt_frame = ttk.Frame(self)
        fmt_frame.pack(pady=10, anchor="w", padx=20)
        ttk.Label(fmt_frame, text="下載格式:").pack(side="left")
        ttk.Radiobutton(fmt_frame, text="MP4 (影片)", variable=self.format_var, value="mp4").pack(side="left", padx=5)
        ttk.Radiobutton(fmt_frame, text="MP3 (音檔)", variable=self.format_var, value="mp3").pack(side="left")
        ttk.Radiobutton(fmt_frame, text="FLAC (無損音檔)", variable=self.format_var, value="flac").pack(side="left", padx=5)

        q_frame = ttk.Frame(self)
        q_frame.pack(pady=(0, 5), anchor="w", padx=20)

        ttk.Label(q_frame, text="畫質:").pack(side="left")
        self.quality_var = tk.StringVar(value="原片最高")
        q_choices = ["360p", "480p", "720p", "1080p", "1440p", "4k", "原片最高"]
        ttk.Combobox(q_frame, textvariable=self.quality_var,
                     values=q_choices, width=10, state="readonly"
                     ).pack(side="left", padx=5)

        pass_frame = ttk.Frame(self)
        pass_frame.pack(pady=(5, 0), anchor="w", padx=20)
        self.need_pass_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pass_frame, text="需要密碼", variable=self.need_pass_var, command=self.toggle_password).pack(side="left")
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(pass_frame, textvariable=self.pass_var, width=25, state="disabled")
        self.pass_entry.pack(side="left", padx=5)

        rate_frame = ttk.Frame(self)
        rate_frame.pack(pady=(5, 0), anchor="w", padx=20)

        ttk.Label(rate_frame, text="限速:").pack(side="left")

        self.rate_choice_var = tk.StringVar(value="0")       
        choices = [str(i) for i in range(1, 11)] + ["自訂"]
        self.rate_combo = ttk.Combobox(
            rate_frame, textvariable=self.rate_choice_var,
            values=choices, width=5, state="readonly"
        )
        self.rate_combo.pack(side="left", padx=(0, 5))
        self.rate_combo.bind("<<ComboboxSelected>>", self.toggle_custom_rate)

        self.custom_rate_var = tk.StringVar()
        self.custom_rate_entry = ttk.Entry(
            rate_frame, textvariable=self.custom_rate_var,
            width=8, state="disabled"
        )
        self.custom_rate_entry.pack(side="left")
        ttk.Label(rate_frame, text="MB/s (0代表全速)").pack(side="left")

        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=15)

        ttk.Button(btn_frame, text="開始下載",
                   command=self.start_download_thread).pack(side="left", padx=6)

        self.pause_btn = ttk.Button(btn_frame, text="暫停",
                                    command=self.toggle_pause,
                                    state="disabled")
        self.pause_btn.pack(side="left", padx=6)

        self.stop_btn = ttk.Button(
            btn_frame, text="中止",
            command=self.stop_download,
            state="disabled"
        )
        self.stop_btn.pack(side="left", padx=6)
        
        self.status_text = tk.Text(self, height=1, width=60,
                                   bd=0, relief="flat",
                                   bg=self.cget("bg"),          
                                   state="disabled")
        self.status_text.pack(pady=3)

        self.title_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.title_var,
                  font=("Helvetica", 10, "bold")).pack(pady=(2, 0))

        self.status_text.tag_config("white",  foreground="#202020")
        self.status_text.tag_config("blue",   foreground="blue")
        self.status_text.tag_config("green",  foreground="green")
        self.status_text.tag_config("yellow", foreground="orange")   

    def toggle_pause(self):
        """
        yt-dlp 的續傳依賴 .part 檔與伺服器 Range 支援，部分來源或直播在「暫停→續傳」未必成功。
        若處於後製（postprocessors，例如轉檔/嵌圖）階段，按「暫停」無法保證狀態回復。
        """
        if not self.current_url:     
            return
        if self.paused:              
            self.paused = False
            self.pause_btn.config(text="暫停")
            self._update_status("重新續傳…")
            threading.Thread(target=self.download,     
                             args=(self.current_url,), daemon=True).start()
        else:                        
            self.pause_flag = True   
            self.pause_btn.config(state="disabled")    

    def stop_download(self):
        if not self.current_url:
            return

        if self.live_proc and self.live_proc.poll() is None:
            try:
                self._kill_live_proc_tree(self.live_proc, polite_timeout=3.0)
            finally:
                
                self.live_proc = None
                self._update_status("已中止（直播錄製已強制停止）。")
                self.pause_btn.config(state="disabled")
                self.stop_btn.config(state="disabled")
                self.current_url = None
                self.paused = False
            return

        self.stop_flag = True
        self.stop_btn.config(state="disabled")

    def toggle_password(self):
        state = "normal" if self.need_pass_var.get() else "disabled"
        self.pass_entry.configure(state=state)

    def toggle_custom_rate(self, _evt=None):
        """
        當選到自訂時才開放輸入欄。
        """
        state = "normal" if self.rate_choice_var.get() == "自訂" else "disabled"
        self.custom_rate_entry.configure(state=state)

    def _apply_rate_limit(self, opts: dict) -> bool:
        """
        依 UI 設定將 ratelimit 寫入 opts。
        回傳 True 表示成功；False 代表格式錯誤，須中止下載。
        """
        value = self.custom_rate_var.get().strip() if self.rate_choice_var.get() == "自訂" else self.rate_choice_var.get()
        if not value or value == "0":         
            return True
        try:
            mbps = float(value)
            if mbps <= 0:
                raise ValueError
            opts['ratelimit'] = int(mbps * 1024 * 1024)   
            return True
        except ValueError:
            self._update_status("限速數字格式錯誤")
            messagebox.showerror(
                "限速格式錯誤",
                "請輸入正數（例：0.5、1、2、2.5）。\n0 代表不限速。"
            )
            return False

    def start_download_thread(self):
        self.title_var.set("")          
        raw_url = self.url_var.get().strip()
        if not raw_url:
            messagebox.showwarning("提醒", "請輸入 URL")
            return
        url = normalize_url(raw_url)
        self._update_status(f"初始化下載 …（{self.quality_var.get()}）")
        threading.Thread(target=self.download, args=(url,), daemon=True).start()

    def download(self, url: str):
        """
        單一路徑完成抓資訊 → 下載並支援暫停/續傳。
        """
        self.current_url = url
        self.pause_flag = False
        self.stop_flag = False
        self.paused = False
        self.after(0, lambda: self.pause_btn.config(state="normal", text="暫停"))
        self.after(0, lambda: self.stop_btn.config(state="normal"))

        try:
            ydl_opts = self._build_base_opts()
            self._add_format_opts(ydl_opts, url)

            fmt_kind = self.format_var.get()
            netloc = urlparse(url).netloc.lower().split(':', 1)[0]
            domain_for_dir = re.sub(r'[^a-z0-9.-]', '_', netloc)
            save_dir = f"{domain_for_dir}_{fmt_kind}"
            os.makedirs(save_dir, exist_ok=True)
            ydl_opts['outtmpl'] = os.path.join(save_dir, '%(title)s.%(ext)s')

            info_opts = {k: v for k, v in ydl_opts.items() if k != 'postprocessors'}
            info_opts.update({'quiet': True, 'skip_download': True})
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            is_live = bool(info.get('is_live'))
            if is_live:
                proc = None
                fmt_kind = self.format_var.get()
                q = self.quality_var.get()
                try:
                    self._update_status("偵測到直播，切換至子程序錄製模式…")

                    cmd = ["yt-dlp", url, "-N", "4"] 

                    ck = os.path.join(os.path.dirname(__file__), 'cookies.txt')
                    if os.path.exists(ck):
                        cmd += ["--cookies", ck]

                    value = self.custom_rate_var.get().strip() if self.rate_choice_var.get() == "自訂" else self.rate_choice_var.get()
                    if value and value != "0":
                        try:
                            mbps = float(value)
                            if mbps > 0:
                                cmd += ["--limit-rate", f"{mbps}M"]
                        except Exception:
                            pass

                    netloc = urlparse(url).netloc.lower()
                    for key, site_opts in SITE_SPECIFIC_OPTS.items():
                        if self._host_matches(netloc, key):
                            if isinstance(site_opts, str):
                                site_opts = SITE_SPECIFIC_OPTS[site_opts]
                            if isinstance(site_opts, dict):
                                if site_opts.get("live_from_start"):
                                    cmd += ["--live-from-start"]
                                if "extractor_args" in site_opts and "twitter" in site_opts["extractor_args"]:
                                    cmd += ["--extractor-args", "twitter:api=syndication"]
                            break

                    netloc = urlparse(url).netloc.lower().split(':', 1)[0]
                    domain_for_dir = re.sub(r'[^a-z0-9.-]', '_', netloc)
                    save_dir = f"{domain_for_dir}_{fmt_kind}"
                    os.makedirs(save_dir, exist_ok=True)
                    outtmpl = os.path.join(save_dir, '%(title)s.%(ext)s')
                    cmd += ["-o", outtmpl]

                    cmd += ["--hls-use-mpegts", "--hls-prefer-ffmpeg"]

                    cmd += ["--downloader-args", "ffmpeg_i:-nostdin -y"]

                    if fmt_kind == "mp3":
                        cmd += ["-f", "bestaudio/best", "-x", "--audio-format", "mp3",
                                "--embed-thumbnail", "--embed-metadata"]
                    elif fmt_kind == "flac":
                        cmd += ["-f", "bestaudio/best", "-x", "--audio-format", "flac",
                                "--embed-thumbnail", "--embed-metadata"]
                    else:
                        if q == "原片最高":
                            cmd += ["-f", "bestvideo*+bestaudio/best"]
                        else:
                            height = 2160 if q.lower() == "4k" else int(q.rstrip("p"))
                            cmd += ["-f",
                                    f"bestvideo[height<={height}]+bestaudio/"
                                    f"best[height<={height}]"]

                    cmd += ["--no-progress", "-q"]
                    creationflags = 0
                    if sys.platform.startswith("win"):
                        CREATE_NEW_PROCESS_GROUP = 0x00000200
                        CREATE_NO_WINDOW = 0x08000000
                        creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

                    kwargs = {}
                    if not sys.platform.startswith("win"):
                        kwargs["preexec_fn"] = os.setsid  
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,          
                        creationflags=creationflags,
                        **kwargs                            
                    )
                    self.live_proc = proc

                    self._update_status("直播錄製中…按「中止」會立刻停止錄製。")
                    self.after(0, lambda: self.pause_btn.config(state="disabled"))
                    self.after(0, lambda: self.stop_btn.config(state="normal"))

                    rc = proc.wait() 

                    if rc == 0:
                        self._update_status("直播錄製結束。")
                    else:
                        self._update_status("直播錄製結束。(把檔案後綴.mp4.part刪除.part留下.mp4)")

                    try:
                        if fmt_kind not in ("mp3", "flac"):
                            latest = None
                            latest_t = -1
                            for fn in os.listdir(save_dir):
                                if fn.lower().endswith(".mp4"):
                                    full = os.path.join(save_dir, fn)
                                    t = os.path.getmtime(full)
                                    if t > latest_t:
                                        latest, latest_t = full, t
                            if latest:
                                if self._try_fix_mp4_inplace(latest):
                                    self._update_status("已修復 MP4 。(把檔案後綴.mp4.part刪除.part留下.mp4)")
                    except Exception:
                        pass

                    self.after(0, lambda: self.stop_btn.config(state="disabled"))
                    self.current_url = None
                    return

                except Exception as e:
                    self._update_status(f"直播子程序啟動或錄製失敗：{strip_ansi(str(e))}")
                    self.after(0, lambda: self.pause_btn.config(state="disabled"))
                    self.after(0, lambda: self.stop_btn.config(state="disabled"))
                    self.current_url = None
                    return

                finally:
                    try:
                        if getattr(self, "live_proc", None) is proc:
                            self.live_proc = None
                    except Exception:
                        self.live_proc = None

            if ydl_opts.get('merge_output_format') == 'mp4':
                v_ext = (info.get('ext') or '').lower()
                if v_ext and v_ext not in ('mp4', 'm4v', 'mov'):
                    ydl_opts.pop('merge_output_format', None)

            self.source_bitrate_kbps = self._get_source_audio_bitrate_kbps(info)
            duration = info.get('duration')

            title = info.get('title', '未知標題')
            self.current_title = title
            self.after(0, lambda: self.title_var.set(f"下載中：{title}"))
            safe_title = ydl_utils.sanitize_filename(title, restricted=False)  
            expected_path = info.get('_filename')

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                if "fragment not found" in str(e).lower():
                    self._update_status("HLS 分片失效，嘗試改用 DASH/mp4 下載…")
                    try:
                        q = self.quality_var.get()
                        if q == "原片最高":
                            fmt_str = "bestvideo[protocol!=m3u8]+bestaudio[protocol!=m3u8]/best[protocol!=m3u8]"
                        else:
                            height = 2160 if q.lower() == "4k" else int(q.rstrip("p"))
                            fmt_str = (
                                f"bestvideo[height<={height}][protocol!=m3u8]+"
                                f"bestaudio[protocol!=m3u8]/best[height<={height}][protocol!=m3u8]"
                            )

                        ydl_opts['format'] = fmt_str
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([url])
                    except Exception as e2:
                        raise e2
                else:
                    raise e

            final_path = self._resolve_final_output_path(
                save_dir, safe_title, self.format_var.get(), self.last_filename, expected_path
            )
            threading.Thread(
                target=self._probe_and_display,
                args=(final_path, duration),
                daemon=True
            ).start()

            self.after(0, lambda: self.pause_btn.config(state="disabled"))
            self.after(0, lambda: self.stop_btn.config(state="disabled"))
            self.current_url = None

        except yt_dlp.utils.DownloadCancelled:
            if self.stop_flag:
                self._update_status("已中止。已下載的部分已保留（.part），可日後續傳。")
                self.after(0, lambda: self.pause_btn.config(state="disabled"))
                self.after(0, lambda: self.stop_btn.config(state="disabled"))
                self.current_url = None
                self.paused = False
                return
            self.paused = True
            self._update_status("已暫停")
            self.after(0, lambda: self.pause_btn.config(text="繼續", state="normal"))

        except Exception as e:
            self.after(0, lambda: self._write_status_plain(f"錯誤：{strip_ansi(str(e))}"))
            self.after(0, self._show_help)
            self.after(0, lambda: self.pause_btn.config(state="disabled"))
            self.after(0, lambda: self.stop_btn.config(state="disabled"))
            self.current_url = None

    def _host_matches(self, netloc: str, key: str) -> bool:
        """
        嚴格比對站點：完全相等或以 .key 結尾（支援子網域）
        """
        net = (netloc or "").split(":", 1)[0].lower()
        k = (key or "").lower()
        return net == k or net.endswith("." + k)

    def _add_password_opts(self, opts: dict):
        """
        依 GUI需要密碼勾選，自動塞入三種參數。
        """
        if self.need_pass_var.get():
            pwd = self.pass_var.get().strip()
            if pwd:
                opts.update({
                    'video_password': pwd,   
                    'videopassword':  pwd,   
                    'http_password':  pwd,   
                })

    def _build_base_opts(self) -> dict:
        opts = {
            'continuedl': True,
            'progress_hooks': [self._hook],
        }
        ck = os.path.join(os.path.dirname(__file__), 'cookies.txt')
        if os.path.exists(ck):
            opts['cookiefile'] = ck
        if not self._apply_rate_limit(opts):
            raise ValueError("限速數字格式錯誤")
        self._add_password_opts(opts)
        return opts

    def _get_source_audio_bitrate_kbps(self, info: dict):
        """
        從 extract_info(...) 的實際將被下載格式推回來源音訊位元率（kbps）。
        先看 requested_formats / requested_downloads（實際選用的音訊 stream），
        找不到再回退到 info / formats。
        """
        def _pick_kbps(fmt, duration=None):
            abr = fmt.get('abr') or fmt.get('audio_bitrate')
            if abr:
                return float(abr)
            tbr = fmt.get('tbr')  
            if tbr:
                return float(tbr)
            try:
                if duration and duration > 0:
                    size = fmt.get('filesize') or fmt.get('filesize_approx')
                    if size:
                        return (float(size) * 8.0 / float(duration)) / 1000.0
            except Exception:
                pass
            return None

        try:
            duration = info.get('duration')

            if info.get('_type') == 'playlist':
                entries = info.get('entries') or []
                if entries:
                    info = entries[0] or {}

            req = info.get('requested_formats') or info.get('requested_downloads')
            if isinstance(req, list) and req:
                audio_fmt = None
                for f in req:
                    v = (f.get('vcodec') or '').lower()
                    a = (f.get('acodec') or '').lower()
                    if v in ('none', '') and a not in ('none', ''):
                        audio_fmt = f
                        break
                if not audio_fmt:
                    audio_fmt = req[-1]
                kbps = _pick_kbps(audio_fmt, duration)
                if kbps:
                    return kbps

            kbps = _pick_kbps(info, duration)
            if kbps:
                return kbps

            formats = info.get('formats') or []
            audio_streams = [f for f in formats if (f.get('vcodec') in (None, 'none'))]
            if audio_streams:
                pref_order = ['m4a', 'mp4a', 'aac', 'opus', 'webm', 'ogg', 'mp3', 'flac', 'wav']
                def _rank(f):
                    ext = (f.get('ext') or '').lower()
                    try:
                        p = pref_order.index(ext)
                    except ValueError:
                        p = len(pref_order)
                    return (p, -float(f.get('abr') or f.get('tbr') or 0))
                audio_streams.sort(key=_rank)
                kbps = _pick_kbps(audio_streams[0], duration)
                if kbps:
                    return kbps
        except Exception:
            pass
        return None

    def _probe_container_bitrate_kbps(self, path: str, duration: float | None):
        """
        先嘗試用 ffprobe 取容器音訊流 bitrate；沒有 ffprobe 就用 filesize/duration 估算。
        """
        if not path or not os.path.exists(path):
            return None

        if not shutil.which('ffprobe') and not self.ffprobe_warned:
            self.ffprobe_warned = True
            messagebox.showwarning(
                "未安裝 ffprobe",
                "將以檔案大小/時長估算音訊位元率，可能不準確。\n"
                "建議安裝 FFmpeg（其中包含 ffprobe）。"
            )

        if shutil.which('ffprobe'):
            try:
                proc = subprocess.run(
                    ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                     '-show_entries', 'stream=bit_rate', '-of', 'default=nw=1:nk=1', path],
                    capture_output=True, text=True, timeout=10
                )
                if proc.returncode == 0:
                    out = proc.stdout.strip()
                    if out.isdigit():
                        return float(out) / 1000.0  
            except Exception:
                pass

        try:
            if duration and duration > 0:
                size_bytes = os.path.getsize(path)
                kbps = (size_bytes * 8.0 / duration) / 1000.0
                return kbps
        except Exception:
            pass
        return None

    def _probe_container_bitrates(self, path: str, duration: float | None):
        """
        回傳 (stream_kbps, avg_kbps, codec_name)
        - stream_kbps: ffprobe 音訊流 bit_rate（若可得）
        - avg_kbps   : 用檔案大小÷時長推得的平均位元率（若可得）
        - codec_name : ffprobe 得到的音訊 codec 名稱（例如 'flac','opus','aac','mp3','pcm_s16le'）
        """
        if not path or not os.path.exists(path):
            return None, None, None

        stream_kbps, avg_kbps, codec = None, None, None

        if not shutil.which('ffprobe') and not self.ffprobe_warned:
            self.ffprobe_warned = True
            messagebox.showwarning(
                "未安裝 ffprobe",
                "將以檔案大小/時長估算音訊位元率，可能不準確。\n"
                "建議安裝 FFmpeg（其中包含 ffprobe）。"
            )

        if shutil.which('ffprobe'):
            try:
                proc = subprocess.run(
                    ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                     '-show_entries', 'stream=codec_name,bit_rate', '-of', 'default=nw=1:nk=1', path],
                    capture_output=True, text=True, timeout=10
                )
                if proc.returncode == 0:
                    lines = [x.strip() for x in proc.stdout.splitlines() if x.strip()]
                    if lines:
                        for x in lines:
                            if x.isdigit():
                                stream_kbps = float(x) / 1000.0
                            else:
                                codec = x
            except Exception:
                pass

        try:
            if duration and duration > 0:
                size_bytes = os.path.getsize(path)
                avg_kbps = (size_bytes * 8.0 / duration) / 1000.0
        except Exception:
            pass

        return stream_kbps, avg_kbps, (codec or None)

    def _probe_and_display(self, final_path: str, duration: float | None):
        stream_kbps, avg_kbps, codec = self._probe_container_bitrates(final_path, duration)

        container_kbps = None
        codec_l = (codec or "").lower()
        if codec_l.startswith("flac") or codec_l.startswith("pcm_"):
            container_kbps = avg_kbps or stream_kbps
        else:
            container_kbps = stream_kbps or avg_kbps

        src_kbps = self.source_bitrate_kbps
        src_str = None
        if src_kbps:
            if container_kbps and src_kbps > container_kbps:
                src_kbps = container_kbps
            src_str = f"{int(round(src_kbps))}"

        def _fmt(x): return f"{int(round(x))}" if x is not None else None

        if container_kbps:
            main = _fmt(container_kbps)
            extra_note = ""
            if stream_kbps and avg_kbps:
                s, a = _fmt(stream_kbps), _fmt(avg_kbps)
                if abs((stream_kbps or 0) - (avg_kbps or 0)) >= 64:
                    if main == s:
                        extra_note = f"（平均≈{a}）"
                    else:
                        extra_note = f"（流≈{s}）"
            if src_str:
                self._update_status(f"來源音訊位元：{src_str}/{main} kbps(封裝原因可能導致檔案數值顯示虛高)")
            else:
                self._update_status(f"下載完成！容器音訊位元率：{main} kbps{extra_note}")
        else:
            if src_str:
                self._update_status(f"來源音訊位元：{src_str} kbps(封裝原因可能導致檔案數值顯示虛高)")
            else:
                self._update_status("下載完成！")

    def _kill_live_proc_tree(self, proc, polite_timeout=3.0):
        """
        終止直播的子程序樹（yt-dlp 與它啟動的 ffmpeg）。
        Windows：
          1) 先送 CTRL_BREAK_EVENT（要求優雅收尾、關檔）
          2) 逾時仍活著 → taskkill /T /F（整棵行程樹）
          3) 仍不收 → kill()
        POSIX：
          1) 送 SIGTERM 到 process group（若無群組就 terminate）
          2) 逾時 → SIGKILL（或 kill）
        """
        if proc is None:
            return

        try:
            if sys.platform.startswith("win"):
                try:
                    os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                except Exception:
                    pass

                try:
                    proc.wait(timeout=polite_timeout)
                    return
                except Exception:
                    pass

                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
                    )
                except Exception:
                    pass

                try:
                    proc.wait(timeout=1.0)
                    return
                except Exception:
                    pass

                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass

            else:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

                try:
                    proc.wait(timeout=polite_timeout)
                    return
                except Exception:
                    pass

                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass

        finally:
            return
    def _try_fix_mp4_inplace(self, path: str) -> bool:
        """
        嘗試把可能缺少完整 moov 的 MP4 以 stream copy 方式重封裝到 *_fixed.mp4，
        若成功就覆蓋回原檔。回傳 True=已修復或不需修復，False=修復失敗。
        """
        try:
            if not shutil.which("ffmpeg"):
                return False
            tmp_out = path[:-4] + "_fixed.mp4"
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-y",
                 "-i", path, "-c", "copy", "-movflags", "+faststart", tmp_out],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=60
            )
            if proc.returncode == 0 and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
                try:
                    os.replace(tmp_out, path)  
                    return True
                except Exception:
                    return False
            if os.path.exists(tmp_out):
                try: os.remove(tmp_out)
                except Exception: pass
            return False
        except Exception:
            return False

    def _add_format_opts(self, opts: dict, url: str):
        netloc = urlparse(url).netloc.lower()

        fmt_kind = self.format_var.get()
        if fmt_kind == "flac":
            opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'flac'}],
                'embedthumbnail': True,
                'embedmetadata': True,
            })
        elif fmt_kind == "mp3":
            opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}],
                'embedthumbnail': True,
                'embedmetadata': True,
            })
        else:
            q = self.quality_var.get()
            if q == "原片最高":
                fmt_str = 'bestvideo*+bestaudio/best'
            else:
                height = 2160 if q.lower() == "4k" else int(q.rstrip("p"))
                fmt_str = (
                    f'bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/'
                    f'best[height<={height}][ext=mp4]/best[height<={height}]'
                )
            opts.update({
                'format': fmt_str,
                'merge_output_format': 'mp4',
                'hls_prefer_native': True,  
                'hls_use_mpegts': True,    
                'nopart': False,           
            })

        for key, site_opts in SITE_SPECIFIC_OPTS.items():
            if self._host_matches(netloc, key):
                if isinstance(site_opts, str):   
                    site_opts = SITE_SPECIFIC_OPTS[site_opts]
                if callable(site_opts):
                    site_opts(self, opts)
                else:
                    opts.update(site_opts)
                break

    def _resolve_final_output_path(self, save_dir: str, title: str, fmt_kind: str, fallback_path: str | None, expected_path: str | None):
        """
        根據目前下載格式（mp4/mp3/flac）與標題，推算真正的最終輸出檔路徑。
        - 音檔：優先找 <title>.mp3 / <title>.flac
        - 影片：沿用 yt-dlp 回報的完成檔
        - 若以上都找不到：在 save_dir 內，用同名檔名前綴或最後修改時間最新做為備選
        """
        title = ydl_utils.sanitize_filename(title, restricted=False) 
        ext = ".mp3" if fmt_kind == "mp3" else (".flac" if fmt_kind == "flac" else None)
        if ext:
            candidate = os.path.join(save_dir, f"{title}{ext}")
            if os.path.exists(candidate):
                return candidate

        if fallback_path and os.path.exists(fallback_path):
            return fallback_path

        if expected_path and os.path.exists(expected_path):
            return expected_path

        try:
            stem = os.path.splitext(os.path.basename(fallback_path or ""))[0] or title
            best = None
            best_mtime = -1
            for fn in os.listdir(save_dir):
                full = os.path.join(save_dir, fn)
                if not os.path.isfile(full):
                    continue
                if fn.startswith(stem) or fn.startswith(title):
                    mt = os.path.getmtime(full)
                    if mt > best_mtime:
                        best, best_mtime = full, mt
            if best:
                return best
        except Exception:
            pass

        try:
            files = [os.path.join(save_dir, f) for f in os.listdir(save_dir)]
            files = [f for f in files if os.path.isfile(f)]
            if files:
                return max(files, key=lambda p: os.path.getmtime(p))
        except Exception:
            pass
        return fallback_path

    def _hook(self, d):
        if self.stop_flag:                  
            raise ydl_utils.DownloadCancelled()
        if self.pause_flag:                 
            raise ydl_utils.DownloadCancelled()
        st = d.get('status')
        if st == 'downloading':
            pct   = strip_ansi(d.get('_percent_str', '')).strip()               
            total = strip_ansi(str(d.get('_total_bytes_str')                         
                               or d.get('_total_bytes_estimate_str',''))).strip()
            speed = strip_ansi(d.get('_speed_str', '')).strip()                  
            eta   = strip_ansi(d.get('_eta_str', '')).strip()                    

            self.after(0, lambda: self._write_status_progress(pct, total, speed, eta))

        elif st == 'finished':
            self.last_filename = d.get('filename')
            self.after(0, lambda: self.pause_btn.config(state="disabled"))
            self.after(0, lambda: self._write_status_plain("轉檔處理中…"))
            self.after(0, lambda: self.title_var.set(f"{self.current_title} 下載完成！"))

    def _update_status(self, text):
        self.after(0, lambda: self._write_status_plain(text))
    
    def _write_status_plain(self, text: str):
        """
        整行單色（black）輸出到 Text。
        """
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        self.status_text.insert("end", text, ("white",))
        self.status_text.configure(state="disabled")

    def _write_status_progress(self, pct, total, speed, eta):
        """
        彩色分段：pct+total=藍、speed=綠、eta=黃，其餘=白。
        """
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")

        self.status_text.insert("end", pct + " ", ("blue",))
        self.status_text.insert("end", "of ",        ("white",))
        self.status_text.insert("end", total + " ",  ("blue",))
        self.status_text.insert("end", "at ",        ("white",))
        self.status_text.insert("end", speed + " ",  ("green",))
        self.status_text.insert("end", "ETA ",       ("white",))
        self.status_text.insert("end", eta,          ("yellow",))

        self.status_text.configure(state="disabled")

    def _show_help(self):
        msg = (
            "常見解決方案:\n\n"
            "● 無法下載 → 先更新 yt‑dlp\n"
            "● 無法合併影音 → 下載ffmpeg\n"
            "● 受限制影片 → 匯出 cookies.txt\n"
            "● 需密碼 → 打勾並輸入密碼\n"
            "● 仍失敗 → 在終端執行 yt-dlp --list-formats <URL> 查看\n"
        )
        self.after(0, lambda: messagebox.showinfo("疑難排解", msg))

if __name__ == '__main__':
    app = YTDownloaderGUI()
    app.mainloop()
