import tkinter as tk
from tkinter import ttk, messagebox
import threading
import os
import re
from urllib.parse import urlparse, parse_qs
import yt_dlp
from yt_dlp import utils as ydl_utils


SITE_SPECIFIC_OPTS = {  #專屬小參數對照表
    "twitcasting.tv": {"live_from_start": True},

    "youtube.com": {
        'writesubtitles': True,
        'subtitleslangs': ['zh-Hant', 'zh-TW'],
        'writeautomaticsub': False,
    },
    "youtu.be": "youtube.com",     # 轉向

    "bilibili.com": {"add_header": "Referer:https://www.bilibili.com/"},
    
    "twitch.tv": {
        "live_from_start": True,
        "download_separate_audio": True,
    },

    "twitter.com": {"extractor_args": "twitter:api=syndication"},
    "x.com": "twitter.com", 
}

#############################################
# 留不留都沒差但就先留著
#############################################

def normalize_url(url: str) -> str:
    """Convert youtu.be / Shorts links to canonical watch?v= form and strip noise params."""
    url = url.strip()
    if not url:
        return url
    if 'youtu.be' in url:
        video_id = urlparse(url).path.lstrip('/')
        return f'https://www.youtube.com/watch?v={video_id}'
    if '/shorts/' in url:
        video_id = url.split('/shorts/')[-1].split('?')[0]
        return f'https://www.youtube.com/watch?v={video_id}'
    parsed = urlparse(url)
    if parsed.netloc.endswith('youtube.com') and parsed.path == '/watch':
        qs = parse_qs(parsed.query)
        if 'v' in qs:
            return f"https://www.youtube.com/watch?v={qs['v'][0]}"
    return url

#############################################
# GUI
#############################################
ANSI_RE = re.compile(r'(?:\x1b\[)?\[[0-9;]*m')   # 抓有/沒有 ESC 的 ANSI 碼

def strip_ansi(txt: str) -> str:
    """去掉 yt‑dlp 內嵌的 ANSI 顏色碼。"""
    return ANSI_RE.sub('', txt)

class YTDownloaderGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video Downloader")
        self.geometry("560x320")
        self.resizable(False, False)
        self.paused = False              # 目前是否處於暫停
        self.pause_flag = False          # 供 hook 讀取的旗標
        self.current_url = None          # 記下正在下載哪支

        # URL input
        ttk.Label(self, text="影片 / 播放清單 URL:").pack(pady=(20, 5), anchor="w", padx=20)
        self.url_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.url_var, width=75).pack(padx=20)

        # 下載格式 
        self.format_var = tk.StringVar(value="mp4")
        fmt_frame = ttk.Frame(self)
        fmt_frame.pack(pady=10, anchor="w", padx=20)
        ttk.Label(fmt_frame, text="下載格式:").pack(side="left")
        ttk.Radiobutton(fmt_frame, text="MP4 (影片)", variable=self.format_var, value="mp4").pack(side="left", padx=5)
        ttk.Radiobutton(fmt_frame, text="MP3 (音檔)", variable=self.format_var, value="mp3").pack(side="left")

        # 畫質 (只影響 MP4)
        q_frame = ttk.Frame(self)
        q_frame.pack(pady=(0, 5), anchor="w", padx=20)

        ttk.Label(q_frame, text="畫質:").pack(side="left")
        self.quality_var = tk.StringVar(value="原片最高")
        q_choices = ["360p", "480p", "720p", "1080p", "1440p", "4k", "原片最高"]
        ttk.Combobox(q_frame, textvariable=self.quality_var,
                     values=q_choices, width=10, state="readonly"
                     ).pack(side="left", padx=5)

        # 需要密碼 (若網站支持)
        pass_frame = ttk.Frame(self)
        pass_frame.pack(pady=(5, 0), anchor="w", padx=20)
        self.need_pass_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pass_frame, text="需要密碼", variable=self.need_pass_var, command=self.toggle_password).pack(side="left")
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(pass_frame, textvariable=self.pass_var, width=25, state="disabled")
        self.pass_entry.pack(side="left", padx=5)

        # --------------------------- 下載限速 ---------------------------
        rate_frame = ttk.Frame(self)
        rate_frame.pack(pady=(5, 0), anchor="w", padx=20)

        ttk.Label(rate_frame, text="限速:").pack(side="left")

        self.rate_choice_var = tk.StringVar(value="0")       # 0 → 不限速
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

        # --------------------------- 下載按鈕 ---------------------------
        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=15)

        ttk.Button(btn_frame, text="開始下載",
                   command=self.start_download_thread).pack(side="left", padx=6)

        self.pause_btn = ttk.Button(btn_frame, text="暫停",
                                    command=self.toggle_pause,
                                    state="disabled")
        self.pause_btn.pack(side="left", padx=6)

        # Status (彩色進度列)
        self.status_text = tk.Text(self, height=1, width=60,
                                   bd=0, relief="flat",
                                   bg=self.cget("bg"),            # 跟視窗同色
                                   state="disabled")
        self.status_text.pack(pady=3)

        self.title_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.title_var,
                  font=("Helvetica", 10, "bold")).pack(pady=(2, 0))

        # 定義顏色 tag
        self.status_text.tag_config("white",  foreground="#202020")
        self.status_text.tag_config("blue",   foreground="blue")
        self.status_text.tag_config("green",  foreground="green")
        self.status_text.tag_config("yellow", foreground="orange")   

    def toggle_pause(self):
        if not self.current_url:     # 沒在下載
            return
        if self.paused:              # 目前是「暫停」狀態，要繼續
            self.paused = False
            self.pause_btn.config(text="暫停")
            self._write_status_plain("重新續傳…")
            threading.Thread(target=self.download,     # 用同一 URL 再叫一次
                             args=(self.current_url,), daemon=True).start()
        else:                        # 目前在下載，要暫停
            self.pause_flag = True   # 交給 hook 去丟例外
            self.pause_btn.config(state="disabled")    # 等 hook 真正停住再改字

    # --------------------------- UI callbacks ---------------------------
    def toggle_password(self):
        state = "normal" if self.need_pass_var.get() else "disabled"
        self.pass_entry.configure(state=state)
        self.pass_entry.configure(state=state)

    def toggle_custom_rate(self, _evt=None):
        """當選到『自訂』時才開放輸入欄。"""
        state = "normal" if self.rate_choice_var.get() == "自訂" else "disabled"
        self.custom_rate_entry.configure(state=state)

    def _apply_rate_limit(self, opts: dict) -> bool:
        """
        依 UI 設定將 ratelimit 寫入 opts。
        回傳 True 表示成功；False 代表格式錯誤，須中止下載。
        """
        value = self.custom_rate_var.get().strip() if self.rate_choice_var.get() == "自訂" else self.rate_choice_var.get()
        if not value or value == "0":          # '0' 或空字串 → 不限速
            return True
        try:
            mbps = float(value)
            if mbps <= 0:
                raise ValueError
            opts['ratelimit'] = int(mbps * 1024 * 1024)   # MB/s 轉 byte/s
            return True
        except ValueError:
            self._update_status("限速數字格式錯誤")
            return False

    def start_download_thread(self):
        self.title_var.set("")          # 清空舊標題
        raw_url = self.url_var.get().strip()
        if not raw_url:
            messagebox.showwarning("提醒", "請輸入 URL")
            return
        url = normalize_url(raw_url)
        self._write_status_plain(f"初始化下載 …（{self.quality_var.get()}）")
        threading.Thread(target=self.download, args=(url,), daemon=True).start()

    # --------------------------- Core download ---------------------------
    def download(self, url: str):
        """單一路徑完成『抓資訊 → 下載』並支援暫停/續傳。"""
        self.current_url = url
        self.pause_flag = False
        self.paused = False
        self.after(0, lambda: self.pause_btn.config(state="normal", text="暫停"))

        try:
            # ---------- 1) 組參數 ----------
            ydl_opts = self._build_base_opts()
            self._add_format_opts(ydl_opts, url)

            # 自動依網域建資料夾
            domain = urlparse(url).netloc.split('.')[-2]
            save_dir = f"{domain}_{self.format_var.get()}"
            os.makedirs(save_dir, exist_ok=True)
            ydl_opts['outtmpl'] = os.path.join(save_dir, '%(title)s.%(ext)s')

            # ---------- 2) 先抓影片資訊（帶 cookies / 密碼） ----------
            info_opts = ydl_opts | {'quiet': True, 'skip_download': True}
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            title = info.get('title', '未知標題')
            self.current_title = title
            self.after(0, lambda: self.title_var.set(f"下載中：{title}"))

            # ---------- 3) 真正下載 ----------
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            self._update_status("下載完成！")
            self.after(0, lambda: self.pause_btn.config(state="disabled"))
            self.current_url = None

        except yt_dlp.utils.DownloadCancelled:
            # 使用者按了「暫停」
            self.paused = True
            self._write_status_plain("已暫停")
            self.after(0, lambda: self.pause_btn.config(text="繼續", state="normal"))

        except Exception as e:
            self._write_status_plain(f"錯誤：{strip_ansi(str(e))}")
            self._show_help()
            self.after(0, lambda: self.pause_btn.config(state="disabled"))
            self.current_url = None

    # --------------------------- Helper ---------------------------
    def _add_password_opts(self, opts: dict):
        """依 GUI『需要密碼』勾選，自動塞入三種參數。"""
        if self.need_pass_var.get():
            pwd = self.pass_var.get().strip()
            if pwd:
                opts.update({
                    'video_password': pwd,   # 通用
                    'videopassword':  pwd,   # 舊名
                    'http_password':  pwd,   # HTTP Basic
                })

    def _build_base_opts(self) -> dict:
        """所有網站共用的 yt‑dlp 參數骨架（cookie/限速/密碼/hook）。"""
        opts = {
            'continuedl': True,
            'progress_hooks': [self._hook],
        }
        # cookies.txt（若存在）
        ck = os.path.join(os.path.dirname(__file__), 'cookies.txt')
        if os.path.exists(ck):
            opts['cookiefile'] = ck
        # 限速
        self._apply_rate_limit(opts)
        # 密碼
        self._add_password_opts(opts)
        return opts

    def _add_format_opts(self, opts: dict, url: str):
        """依 GUI 資訊（MP4/MP3、畫質）與站點特性補參數。"""
        netloc = urlparse(url).netloc.lower()

        # --- 音檔 (MP3) ---
        if self.format_var.get() == "mp3":
            opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
            return

        # --- 影片 (MP4) ---
        q = self.quality_var.get()
        if q == "原片最高":
            fmt_str = 'bestvideo+bestaudio/best'
        else:
            height = 2160 if q.lower() == "4k" else int(q.rstrip("p"))
            fmt_str = (f'bestvideo[height<={height}]+bestaudio/'
                       f'best[height<={height}]')
        opts.update({
            'format': fmt_str,
            'merge_output_format': 'mp4',
        })

        # --------------------------- 站點專屬參數 ---------------------------
        for key, site_opts in SITE_SPECIFIC_OPTS.items():
            if key in netloc:
                # 轉向：值是一條字串 → 重新索引取實際 dict
                if isinstance(site_opts, str):
                    site_opts = SITE_SPECIFIC_OPTS[site_opts]

                if callable(site_opts):             # 函式 → 執行
                    site_opts(self, opts)
                else:                               # dict → 直接更新
                    opts.update(site_opts)
                break

    def _hook(self, d):
        if self.pause_flag:                 # 主執行緒按了「暫停」
            raise ydl_utils.DownloadCancelled()
        st = d.get('status')
        if st == 'downloading':
            pct   = strip_ansi(d.get('_percent_str', '')).strip()                # %
            total = strip_ansi(d.get('_total_bytes_str')                         # GiB
                               or d.get('_total_bytes_estimate_str','')).strip()
            speed = strip_ansi(d.get('_speed_str', '')).strip()                  # MiB/s
            eta   = strip_ansi(d.get('_eta_str', '')).strip()                    # 預計下載剩餘時間

            self.after(0, lambda: self._write_status_progress(pct, total, speed, eta))

        elif st == 'finished':
            self.after(0, lambda: self._write_status_plain("轉檔處理中…"))
            self.after(0, lambda: self.title_var.set(f"{self.current_title} 下載完成！"))

    def _update_status(self, text):
        self.after(0, lambda: self._write_status_plain(text))
    
    def _write_status_plain(self, text: str):
        """整行單色（black）輸出到 Text。"""
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        self.status_text.insert("end", text, ("white",))
        self.status_text.configure(state="disabled")

    def _write_status_progress(self, pct, total, speed, eta):
        """彩色分段：pct+total=藍、speed=綠、eta=黃，其餘=白。"""
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
            "● 無法合併影音 → 下載ffmpeg"
            "● 受限制影片 → 匯出 cookies.txt\n"
            "● 需密碼 → 打勾並輸入密碼\n"
            "● 仍失敗 → 在終端執行 yt-dlp --list-formats <URL> 查看\n"
        )
        self.after(0, lambda: messagebox.showinfo("疑難排解", msg))

if __name__ == '__main__':
    app = YTDownloaderGUI()
    app.mainloop()
