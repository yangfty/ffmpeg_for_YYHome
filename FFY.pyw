# -*- coding: utf-8 -*-
"""
FFY · ffmpeg_for_YYHome 影片批量转码工具 · V0.1.0
==================================================
适配目标（当贝 New F3 投影 1080p SDR + 索尼 HT-NT5 回音壁，HDMI ARC 连接）：
  容器    : MKV
  视频    : H.264 High / BT.709 / SDR / 8bit，分辨率与帧率保持原样
  HDR     : 自动检测 HDR10/HLG -> SDR（zscale + tonemap 色调映射，默认 mobius）
  音频    : 自动决策 —— DTS / AC3 直通保留；DD+ / TrueHD / Atmos / 多声道LPCM /
            AAC / FLAC 等自动转 Dolby Digital AC3 5.1（HDMI ARC 仅能传输
            DD / DTS / 2.0PCM，故统一落到 AC3）
  字幕    : SRT/ASS 原样保留（mov_text/webvtt 自动转 SRT，附件字体保留）
  加速    : AMD 显卡 d3d11va 硬件解码 + h264_amf 硬件编码
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import queue
from dataclasses import dataclass, field, asdict

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
APP_TITLE = "FFY · ffmpeg_for_YYHome"
APP_VER = "V0.1.3"
DEFAULT_FFMPEG = r"C:\Installed\FFmpeg\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FFY_config.json")
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FFY_logs")

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".ts",
              ".m2ts", ".mts", ".tp", ".webm", ".mpg", ".mpeg", ".vob", ".rmvb", ".3gp"}

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
BT2020_SET = {"bt2020", "bt2020nc"}
SUB_TO_SRT = {"mov_text", "webvtt", "text"}

# 音频自动策略：这些编码可经 HDMI ARC 原样直通到 HT-NT5（DTS 家族 ffprobe 统一报 dts）
AUDIO_PASSTHROUGH = {"dts", "ac3"}

# 色调映射：按推荐优先级排序，前三个带星标
TONEMAP_ORDER = ["mobius", "hable", "bt2390", "reinhard", "gamma"]
TONEMAP_STARRED = {"mobius", "hable", "bt2390"}
X264_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow"]

# 状态
ST_WAIT_PROBE = "待检测"
ST_PROBING = "检测中"
ST_READY = "就绪"
ST_RUNNING = "转码中"
ST_DONE = "完成"
ST_FAIL = "失败"
ST_SKIP = "跳过"
ST_ABORT = "中止"

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY = 0x00004000

# ---- 亮色主题色板 ----
C_BG = "#F3F5F9"          # 窗口底
C_CARD = "#FFFFFF"        # 卡片
C_FIELD = "#EEF1F6"       # 输入控件/表格底
C_HOVER = "#E4E9F3"
C_BORDER = "#E3E8F1"
C_TEXT = "#232A38"
C_MUT = "#5B6478"
C_DIM = "#9AA3B5"
C_ACCENT = "#4F6DF5"      # 主色（按钮/进度）
C_ACCENT_D = "#3E5BE6"    # 悬浮
C_ACCENT_P = "#3348C2"    # 按压
C_OK = "#18A05E"
C_ERR = "#E5484D"
C_WARN = "#E7830A"


# ----------------------------------------------------------------------------
# 设置
# ----------------------------------------------------------------------------
@dataclass
class Settings:
    ffmpeg_path: str = DEFAULT_FFMPEG
    encoder: str = "amf"            # "amf" | "x264"
    qp_i: int = 18
    qp_p: int = 20
    crf: int = 18
    x264_preset: str = "medium"
    hw_decode: bool = True
    hdr2sdr: bool = True
    tonemap_algo: str = "mobius"
    audio_policy: str = "auto"      # "auto" | "copy_all" | "ac3_all"
    uhd_policy: str = "smart"       # "smart"(4K HEVC 保留4K) | "h264_1080"(一律降1080p)
    skip_compliant: bool = True
    out_mode: str = "subfolder"     # "subfolder" | "same" | "custom"
    out_subfolder: str = "FFY_输出"
    out_custom: str = ""
    overwrite: bool = False

    def validate(self):
        return bool(self.ffmpeg_path) and os.path.isfile(self.ffmpeg_path)


def load_settings():
    s = Settings()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if hasattr(s, k):
                setattr(s, k, v)
    except Exception:
        pass
    return s


def save_settings(s: Settings):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(asdict(s), f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# ffprobe 探测
# ----------------------------------------------------------------------------
def get_ffprobe_path(ffmpeg_path):
    p = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe.exe")
    return p if os.path.isfile(p) else "ffprobe"


def parse_fps(rate):
    try:
        if rate and "/" in rate:
            a, b = rate.split("/", 1)
            a, b = float(a), float(b)
            return round(a / b, 3) if b else 0.0
        return float(rate)
    except Exception:
        return 0.0


def pix_depth(pix_fmt, bits_per_raw):
    if bits_per_raw:
        try:
            return int(bits_per_raw)
        except Exception:
            pass
    m = re.search(r"(\d+)(?:le|be)?$", pix_fmt or "")
    return int(m.group(1)) if m else 8


def probe_file(ffmpeg_path, path, timeout=120):
    ffprobe = get_ffprobe_path(ffmpeg_path)
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[-500:] or "ffprobe 失败")
    data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    fmt = data.get("format", {})

    video, audios, subs = None, [], []
    for st in data.get("streams", []):
        ct = st.get("codec_type")
        if ct == "video":
            if video is None and not st.get("disposition", {}).get("attached_pic"):
                video = st
        elif ct == "audio":
            audios.append(st)
        elif ct == "subtitle":
            subs.append(st)
    if video is None:
        raise RuntimeError("未找到视频流")

    def to_f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    duration = to_f(fmt.get("duration")) or to_f(video.get("duration"))
    size = to_f(fmt.get("size"))
    transfer = (video.get("color_transfer") or "").lower()
    primaries = (video.get("color_primaries") or "").lower()
    matrix = (video.get("color_space") or "").lower()
    is_hdr = (transfer in HDR_TRANSFERS) or (primaries in BT2020_SET) or (matrix in BT2020_SET)
    depth = pix_depth(video.get("pix_fmt"), video.get("bits_per_raw_sample"))
    fps = parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")

    audio_tracks = []
    for a in audios:
        profile = a.get("profile") or ""
        atmos = "atmos" in profile.lower()
        audio_tracks.append({
            "codec": a.get("codec_name", "?"),
            "profile": profile,
            "atmos": atmos,
            "ch": int(a.get("channels", 0) or 0),
            "sr": int(a.get("sample_rate", 0) or 0),
            "lang": (a.get("tags", {}) or {}).get("language", ""),
        })

    info = {
        "path": path,
        "name": os.path.basename(path),
        "duration": duration,
        "size": size,
        "vcodec": video.get("codec_name", "?"),
        "vprofile": video.get("profile", ""),
        "width": video.get("width", 0),
        "height": video.get("height", 0),
        "fps": fps,
        "pix_fmt": video.get("pix_fmt", ""),
        "depth": depth,
        "transfer": transfer,
        "primaries": primaries,
        "matrix": matrix,
        "is_hdr": is_hdr,
        "hdr_type": ("HDR10" if transfer == "smpte2084" else
                     "HLG" if transfer == "arib-std-b67" else
                     "BT.2020") if is_hdr else "",
        "audios": audio_tracks,
        "subs": [(s.get("codec_name", "?"),
                  (s.get("tags", {}) or {}).get("language", "")) for s in subs],
    }
    info["compliant"] = (info["vcodec"] == "h264" and not is_hdr and depth <= 8)
    return info


# ----------------------------------------------------------------------------
# 音频自动策略
# ----------------------------------------------------------------------------
def audio_track_plan(track, policy):
    """返回单条音轨的处理决策：("copy", None) 或 ("ac3", (channels, rate))"""
    codec = track["codec"]
    if policy == "copy_all":
        return ("copy", None)
    if policy == "ac3_all":
        return ("ac3", (min(track["ch"] or 2, 6), min(track["sr"] or 48000, 48000)))
    # auto：DTS 家族 / AC3 可经 ARC 直通，其余（DD+、TrueHD、Atmos、LPCM、AAC…）转 AC3
    if codec in AUDIO_PASSTHROUGH:
        return ("copy", None)
    return ("ac3", (min(track["ch"] or 2, 6), min(track["sr"] or 48000, 48000)))


def hdr_filter_chain(algo):
    return ["format=yuv420p16le",
            "zscale=t=linear:npl=100",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=tonemap=%s:desat=0" % algo,
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p"]


def is_uhd(info):
    return (info.get("width") or 0) > 1920 or (info.get("height") or 0) > 1080


def keep_hevc_4k(info, cfg: Settings):
    """4K HEVC 片源保留 4K 输出 HEVC（投影 HEVC 硬解 4K@60，体验最佳）"""
    return (cfg.uhd_policy == "smart" and is_uhd(info)
            and info.get("vcodec") in ("hevc", "h265"))


def audio_all_passthrough(info, cfg: Settings):
    """全部音轨都无需重编码（直通）时，配合视频已达标可整体跳过"""
    if cfg.audio_policy == "copy_all":
        return True
    if cfg.audio_policy == "ac3_all":
        return False
    return all(tr["codec"] in AUDIO_PASSTHROUGH for tr in info["audios"])


def build_cmd(info, cfg: Settings, out_path):
    cmd = [cfg.ffmpeg_path, "-hide_banner", "-nostdin", "-y",
           "-loglevel", "warning", "-nostats"]
    if cfg.hw_decode:
        cmd += ["-hwaccel", "d3d11va"]

    cmd += ["-i", info["path"],
            "-map", "0:V:0",
            "-map", "0:a?",
            "-map", "0:s?",
            "-map", "0:t?",
            "-map_chapters", "0",
            "-map_metadata", "0"]

    tag_709 = False
    if cfg.hdr2sdr and info["is_hdr"]:
        filters = hdr_filter_chain(cfg.tonemap_algo)
        tag_709 = True
    elif info["primaries"] in BT2020_SET or info["matrix"] in BT2020_SET:
        filters = ["format=yuv420p16le", "zscale=t=bt709:p=bt709:m=bt709:r=tv", "format=yuv420p"]
        tag_709 = True
    else:
        filters = ["format=yuv420p"]
    # 超 1080p 且不保留 4K HEVC 时降到 1080p（投影 H.264 硬解仅 4K@30，会卡）
    if is_uhd(info) and not keep_hevc_4k(info, cfg):
        filters = [("scale=-2:1080" if (info.get("height") or 0) > 1080
                    else "scale=1920:-2")] + filters
    cmd += ["-vf", ",".join(filters)]

    if keep_hevc_4k(info, cfg):
        # 4K HEVC 保留：输出 HEVC（投影硬解 4K@60）
        if cfg.encoder == "amf":
            cmd += ["-c:v", "hevc_amf", "-quality", "quality", "-rc", "cqp",
                    "-qp_i", str(int(cfg.qp_i) + 2), "-qp_p", str(int(cfg.qp_p) + 2)]
        else:
            cmd += ["-c:v", "libx265", "-preset", cfg.x264_preset,
                    "-crf", str(int(cfg.crf) + 2)]
    elif cfg.encoder == "amf":
        cmd += ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                "-qp_i", str(int(cfg.qp_i)), "-qp_p", str(int(cfg.qp_p)),
                "-profile:v", "high"]
    else:
        cmd += ["-c:v", "libx264", "-profile:v", "high",
                "-preset", cfg.x264_preset, "-crf", str(int(cfg.crf))]
    cmd += ["-pix_fmt", "yuv420p"]
    if tag_709:
        cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]

    # 音频：逐轨自动决策
    for i, track in enumerate(info["audios"]):
        act, param = audio_track_plan(track, cfg.audio_policy)
        if act == "copy":
            cmd += ["-c:a:%d" % i, "copy"]
        else:
            ch, sr = param
            cmd += ["-c:a:%d" % i, "ac3", "-b:a:%d" % i, "640k"]
            if track["ch"] > 6:
                cmd += ["-ac:a:%d" % i, str(ch)]
            if track["sr"] > 48000:
                cmd += ["-ar:a:%d" % i, str(sr)]

    for i, (codec, _lang) in enumerate(info["subs"]):
        cmd += ["-c:s:%d" % i, ("srt" if codec in SUB_TO_SRT else "copy")]

    cmd += ["-c:t", "copy",
            "-progress", "pipe:1",
            out_path]
    return cmd


def resolve_out_path(info, cfg: Settings, task=None):
    src = info["path"]
    stem = os.path.splitext(os.path.basename(src))[0]
    if stem.lower().endswith(".ffy"):
        stem = stem[:-4]
    if task is not None and getattr(task, "out_dir", ""):
        # 文件夹批量任务：统一输出到指定目录（忽略原层级），重名加后缀
        d = task.out_dir
        stem += getattr(task, "out_suffix", "") or ""
    elif cfg.out_mode == "subfolder":
        d = os.path.join(os.path.dirname(src), cfg.out_subfolder or "FFY_输出")
    elif cfg.out_mode == "same":
        d = os.path.dirname(src)
    else:
        d = cfg.out_custom or os.path.dirname(src)
    out = os.path.join(d, stem + ".mkv")
    if os.path.abspath(out) == os.path.abspath(src):
        out = os.path.join(d, stem + ".FFY.mkv")
    return out


def parse_out_time_sec(d):
    v = d.get("out_time_us") or d.get("out_time_ms") or d.get("out_time")
    if not v or v == "N/A":
        return None
    if ":" in v:
        try:
            h, m, s = v.split(":")[:3]
            return int(h) * 3600 + int(m) * 60 + float(s)
        except Exception:
            return None
    try:
        return int(v) / 1e6
    except Exception:
        return None


# ----------------------------------------------------------------------------
# 任务
# ----------------------------------------------------------------------------
@dataclass
class Task:
    iid: str
    path: str
    info: dict = None
    out_dir: str = ""       # 文件夹批量任务的统一输出目录（空=跟随设置）
    out_suffix: str = ""    # 平铺重名时追加的后缀，如 " (2)"
    status: str = ST_WAIT_PROBE
    progress: float = 0.0
    speed: str = ""
    out_path: str = ""
    out_size: int = 0
    elapsed: float = 0.0
    note: str = ""
    cmd: list = field(default_factory=list)
    err_tail: list = field(default_factory=list)
    t_start: float = 0.0

    def reset(self):
        self.status = ST_READY if self.info else ST_WAIT_PROBE
        self.progress = 0.0
        self.speed = ""
        self.note = ""
        self.err_tail = []


def fmt_duration(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return "--:--"
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%02d:%02d" % (m, s))


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


def fmt_res(w, h):
    if not w or not h:
        return ""
    if abs(w / h - 16 / 9) < 0.02:
        tag = {1920: "1080p", 1280: "720p", 3840: "4K", 7680: "8K"}.get(w, "")
        if tag:
            return tag
    return "%dx%d" % (w, h)


AUDIO_NAMES = {"dts": "DTS", "ac3": "DD 5.1", "eac3": "DD+", "truehd": "TrueHD",
               "flac": "FLAC", "aac": "AAC", "mp3": "MP3", "opus": "Opus",
               "vorbis": "Vorbis", "pcm_s16le": "LPCM", "pcm_s24le": "LPCM",
               "alac": "ALAC", "dsd": "DSD"}


def audio_track_label(track):
    name = AUDIO_NAMES.get(track["codec"], track["codec"].upper())
    if track["atmos"]:
        name += " Atmos"
    elif track["codec"] == "dts" and track["profile"] and track["profile"] not in ("DTS", "dts"):
        p = track["profile"].replace("DTS-", "").replace("DTS ", "")
        if p and p.lower() != "dts":
            name = "DTS " + p.split(" ")[0]
    ch = {1: "2.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(track["ch"], "%dch" % track["ch"]) if track["ch"] else ""
    return name + ((" " + ch) if ch else "")


def video_desc(info):
    if not info:
        return "…"
    codec = {"hevc": "H.265", "h264": "H.264"}.get(info["vcodec"], info["vcodec"].upper())
    parts = [codec]
    if info["depth"] > 8:
        parts.append("%dbit" % info["depth"])
    if info["is_hdr"]:
        parts.append(info["hdr_type"])
    res = fmt_res(info["width"], info["height"])
    if res:
        parts.append(res)
    return " · ".join(parts)


def audio_desc(info, cfg: Settings = None):
    if not info:
        return "…"
    if not info["audios"]:
        return "无音轨"
    outs = []
    for t in info["audios"]:
        label = audio_track_label(t)
        if cfg is not None and info.get("compliant") is not None:
            act, _p = audio_track_plan(t, cfg.audio_policy if cfg else "auto")
            label += " ✓直通" if act == "copy" else " →AC3"
        outs.append(label)
    return "、".join(outs)


def sub_desc(info):
    if not info:
        return "…"
    if not info["subs"]:
        return "无"
    return "、".join({"subrip": "SRT", "ass": "ASS", "ssa": "ASS", "hdmv_pgs_subtitle": "PGS",
                      "dvd_subtitle": "VobSub", "mov_text": "TX3G", "webvtt": "VTT"}.get(c, c.upper())
                     for c, _l in info["subs"])


def action_desc(info, cfg: Settings):
    if not info:
        return ""
    if info["compliant"] and audio_all_passthrough(info, cfg):
        return "无需转码" if cfg.skip_compliant else "重转"
    if keep_hevc_4k(info, cfg):
        base = "4K HEVC 保留"
    elif is_uhd(info) and cfg.uhd_policy == "smart":
        base = "降 1080p"
    else:
        base = "转码"
    if info["is_hdr"] and cfg.hdr2sdr:
        base += " · HDR→SDR"
    return base


# ----------------------------------------------------------------------------
# 圆角药丸大按钮（悬浮加深 · 按压下沉，点按反馈明显）
# ----------------------------------------------------------------------------
BTN_KINDS = {
    "primary": {"fill": C_ACCENT,  "hover": C_ACCENT_D, "press": C_ACCENT_P,
                "fg": "#FFFFFF",   "line": None,        "shadow": "#C3CFFB"},
    "soft":    {"fill": C_CARD,    "hover": "#F2F5FB",  "press": "#E1E8F6",
                "fg": C_TEXT,      "line": C_BORDER,    "shadow": "#D8DFEC"},
    "danger":  {"fill": "#FFF1F1", "hover": "#FFE4E5",  "press": "#FFD8DA",
                "fg": C_ERR,       "line": "#F3C7C9",   "shadow": "#EFC4C6"},
}


def _round_rect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class CuteButton(tk.Canvas):
    """圆角大按钮：悬浮变深、按压整体下沉 2px（底部阴影被盖住），反馈明显"""
    _NORMAL, _HOVER, _PRESS = 0, 1, 2

    def __init__(self, master, text="按钮", command=None, kind="soft",
                 height=44, padx=24, size=11, bold=True, bg=C_CARD):
        super().__init__(master, highlightthickness=0, bd=0, cursor="hand2", bg=bg)
        self._kind = BTN_KINDS[kind]
        self._command = command
        self._enabled = True
        self._state = self._NORMAL
        self._h = height
        self._padx = padx
        weight = "bold" if bold else "normal"
        self._font = tkfont.Font(family="Microsoft YaHei UI", size=size, weight=weight)
        self.set_text(text)
        self.bind("<Enter>", lambda e: self._set(self._HOVER))
        self.bind("<Leave>", lambda e: self._set(self._NORMAL))
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_text(self, text):
        self._text = text
        self._bw = self._font.measure(text) + self._padx * 2
        self.configure(width=self._bw, height=self._h + 5)
        self._redraw()

    def set_enabled(self, on):
        self._enabled = bool(on)
        self._state = self._NORMAL
        self.configure(cursor="hand2" if on else "arrow")
        self._redraw()

    def _on_press(self, _e):
        if self._enabled:
            self._set(self._PRESS)

    def _on_release(self, _e):
        if not self._enabled:
            return
        fired = self._state == self._PRESS
        self._set(self._HOVER if self._over_pointer() else self._NORMAL)
        if fired and self._command:
            try:
                self._command()
            except Exception:
                pass

    def _over_pointer(self):
        x, y = self.winfo_pointerxy()
        cx, cy = self.winfo_rootx(), self.winfo_rooty()
        w, h = self.winfo_width(), self.winfo_height()
        return cx <= x < cx + w and cy <= y < cy + h

    def _set(self, st):
        if not self._enabled:
            st = self._NORMAL
        if st != self._state:
            self._state = st
            self._redraw()

    def _redraw(self):
        self.delete("all")
        k = self._kind
        w, h = self._bw, self._h
        pressed = self._state == self._PRESS and self._enabled
        if not self._enabled:
            fill, fg, line = "#EDF0F6", C_DIM, "#E4E8F0"
        else:
            fill = k["press"] if pressed else (
                k["hover"] if self._state == self._HOVER else k["fill"])
            fg, line = k["fg"], k["line"]
        top, r = 1, (h - 2) // 2
        if self._enabled and not pressed:
            # 底部阴影层：按压时按钮下沉把它盖住，形成“按下去”的手感
            _round_rect(self, 2, top + 3, w - 2, top + h + 2, r,
                        fill=k["shadow"], outline="")
        if pressed:
            top += 2
        _round_rect(self, 2, top, w - 2, top + h, r, fill=fill,
                    outline=(line or fill), width=1)
        self.create_text(w // 2, top + h // 2 + 1, text=self._text,
                         font=self._font, fill=fg)


def pill_badge(parent, text, fg, bg, panel=C_CARD, size=9, height=22):
    """小圆角徽章（如版本号）"""
    f = tkfont.Font(family="Microsoft YaHei UI", size=size, weight="bold")
    w = f.measure(text) + 18
    cv = tk.Canvas(parent, width=w, height=height, bg=panel,
                   highlightthickness=0, bd=0)
    _round_rect(cv, 1, 2, w - 1, height - 2, (height - 4) // 2,
                fill=bg, outline="")
    cv.create_text(w // 2, height // 2 + 1, text=text, font=f, fill=fg)
    return cv


# ----------------------------------------------------------------------------
# 主界面（亮色现代风）
# ----------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_settings()

        self.tasks = {}
        self.iid_seq = 0
        self.msg_q = queue.Queue()
        self.enc_thread = None
        self.stop_flag = False
        self.proc = None
        self.batch_running = False
        self.log_file = None

        self._setup_style()
        self._build_ui()
        self._load_cfg_to_ui()
        self._poll()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- 样式
    def _setup_style(self):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        F = "Microsoft YaHei UI"
        self.root.configure(bg=C_BG)
        self.root.option_add("*Font", (F, 10))
        self.root.option_add("*Background", C_BG)
        self.root.option_add("*Foreground", C_TEXT)

        # Windows 10/11 浅色标题栏
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            val = ctypes.c_int(0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), 4)
        except Exception:
            pass

        SEL = "#DCE4FA"   # 浅色选中
        style.configure(".", background=C_BG, foreground=C_TEXT, fieldbackground=C_CARD,
                        bordercolor=C_BORDER, lightcolor=C_CARD, darkcolor=C_CARD,
                        troughcolor=C_FIELD, arrowcolor=C_MUT, font=(F, 10))
        style.map(".", background=[("selected", SEL)],
                  foreground=[("selected", C_TEXT)])

        style.configure("TFrame", background=C_BG)
        style.configure("Card.TFrame", background=C_CARD)
        style.configure("TLabel", background=C_BG, foreground=C_TEXT)
        style.configure("Dim.TLabel", background=C_BG, foreground=C_MUT)
        style.configure("Tiny.TLabel", background=C_BG, foreground=C_DIM, font=(F, 9))
        style.configure("Card.TLabel", background=C_CARD, foreground=C_TEXT)
        style.configure("CardDim.TLabel", background=C_CARD, foreground=C_MUT, font=(F, 9))

        # 普通 ttk 按钮（仅弹窗等次要位置使用；主界面按钮见 CuteButton）
        style.configure("TButton", background=C_CARD, foreground=C_TEXT,
                        borderwidth=1, relief="solid", bordercolor=C_BORDER,
                        focusthickness=0, padding=(12, 7), font=(F, 10))
        style.map("TButton",
                  background=[("pressed", C_HOVER), ("active", C_HOVER),
                              ("disabled", C_FIELD)],
                  bordercolor=[("pressed", C_ACCENT), ("active", C_ACCENT)],
                  foreground=[("disabled", C_DIM)])

        style.configure("TCheckbutton", background=C_BG, foreground=C_TEXT,
                        focuscolor=C_BG, font=(F, 10))
        style.map("TCheckbutton", background=[("active", C_BG)],
                  indicatorcolor=[("selected", C_ACCENT), ("!selected", "#FFFFFF")])
        style.configure("Card.TCheckbutton", background=C_CARD, foreground=C_TEXT,
                        focuscolor=C_CARD, font=(F, 10))
        style.map("Card.TCheckbutton", background=[("active", C_CARD)],
                  indicatorcolor=[("selected", C_ACCENT), ("!selected", "#FFFFFF")])

        style.configure("TCombobox", fieldbackground=C_CARD, background=C_CARD,
                        foreground=C_TEXT, arrowcolor=C_MUT, bordercolor=C_BORDER,
                        lightcolor=C_CARD, darkcolor=C_CARD, borderwidth=1,
                        padding=(8, 5), selectbackground=SEL,
                        selectforeground=C_TEXT)
        style.map("TCombobox",
                  fieldbackground=[("readonly", C_CARD), ("disabled", C_FIELD)],
                  foreground=[("disabled", C_DIM)])
        self.root.option_add("*TCombobox*Listbox.background", C_CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", C_TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SEL)
        self.root.option_add("*TCombobox*Listbox.selectForeground", C_TEXT)
        self.root.option_add("*TCombobox*Listbox.font", (F, 10))

        style.configure("TSpinbox", fieldbackground=C_CARD, foreground=C_TEXT,
                        background=C_CARD, arrowcolor=C_MUT, bordercolor=C_BORDER,
                        lightcolor=C_CARD, darkcolor=C_CARD, borderwidth=1,
                        padding=(4, 3))
        style.map("TSpinbox", fieldbackground=[("readonly", C_CARD)],
                  foreground=[("disabled", C_DIM)],
                  background=[("active", C_HOVER)])

        style.configure("Treeview", background=C_CARD, foreground=C_TEXT,
                        fieldbackground=C_CARD, rowheight=34, borderwidth=0,
                        font=(F, 10))
        style.configure("Treeview.Heading", background="#F5F7FB", foreground=C_MUT,
                        borderwidth=0, relief="flat", font=(F, 9, "bold"), padding=(8, 8))
        style.map("Treeview", background=[("selected", SEL)],
                  foreground=[("selected", C_TEXT)])
        style.map("Treeview.Heading", background=[("active", C_HOVER)])

        style.configure("Vertical.TScrollbar", background="#C9D2E4", troughcolor=C_CARD,
                        bordercolor=C_CARD, lightcolor=C_CARD, darkcolor=C_CARD,
                        arrowcolor=C_MUT, relief="flat")
        style.map("Vertical.TScrollbar", background=[("active", "#B7C2D9")])
        style.configure("Horizontal.TScrollbar", background="#C9D2E4", troughcolor=C_CARD,
                        bordercolor=C_CARD, lightcolor=C_CARD, darkcolor=C_CARD,
                        arrowcolor=C_MUT, relief="flat")
        style.map("Horizontal.TScrollbar", background=[("active", "#B7C2D9")])
        style.configure("TProgressbar", background=C_ACCENT, troughcolor=C_FIELD,
                        borderwidth=0, thickness=18)
        style.configure("TLabelframe", background=C_CARD, bordercolor=C_BORDER)
        style.configure("TLabelframe.Label", background=C_CARD, foreground=C_MUT,
                        font=(F, 9, "bold"))

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        self.root.title("%s %s" % (APP_TITLE, APP_VER))
        self.root.geometry("1180x740")
        self.root.minsize(980, 660)

        outer = ttk.Frame(self.root, padding=(0, 0))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # ---- 顶部标题栏 ----
        header = ttk.Frame(outer, style="Card.TFrame", padding=(20, 14))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(2, weight=1)
        ttk.Label(header, text="FFY", style="Card.TLabel",
                  font=("Microsoft YaHei UI", 20, "bold"), foreground=C_ACCENT
                  ).grid(row=0, column=0, sticky="w")
        pill_badge(header, APP_VER, fg=C_ACCENT, bg="#E8EDFE",
                   panel=C_CARD).grid(row=0, column=1, sticky="ws", padx=(10, 0), pady=(7, 0))
        ttk.Label(header, text="ffmpeg_for_YYHome · 影片自动转码", style="CardDim.TLabel",
                  font=("Microsoft YaHei UI", 10)
                  ).grid(row=0, column=2, sticky="ws", padx=(12, 0), pady=(4, 0))
        ttk.Label(header,
                  text="自动输出：MKV · H.264 · BT.709 SDR · DTS/AC3 直通 · 其余音频转 AC3 5.1",
                  style="CardDim.TLabel").grid(row=1, column=2, sticky="w", padx=(12, 0))
        self.lbl_ff = ttk.Label(header, text="", style="CardDim.TLabel")
        self.lbl_ff.grid(row=0, column=3, rowspan=2, sticky="e")

        # ---- 主卡片：文件列表 ----
        main = ttk.Frame(outer, style="Card.TFrame", padding=14)
        main.grid(row=2, column=0, sticky="nsew", padx=12, pady=(10, 0))
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        bar = ttk.Frame(main, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        CuteButton(bar, text="＋ 添加影片", kind="primary", height=46, size=12,
                   bg=C_CARD, command=self.add_files).pack(side="left")
        CuteButton(bar, text="⌕ 扫描文件夹", height=46, bg=C_CARD,
                   command=self.add_folder).pack(side="left", padx=(10, 0))
        CuteButton(bar, text="↻ 重新检测", height=46, bg=C_CARD,
                   command=self.reprobe_selected).pack(side="left", padx=(10, 0))
        CuteButton(bar, text="✕ 移除", height=46, bg=C_CARD,
                   command=self.remove_selected).pack(side="left", padx=(10, 0))
        self.btn_adv = CuteButton(bar, text="⚙ 高级选项", height=46, bg=C_CARD,
                                  command=self.toggle_advanced)
        self.btn_adv.pack(side="right")

        cols = ("status", "progress", "video", "audio", "subs", "dur", "size", "action")
        texts = {"status": "状态", "progress": "进度", "video": "视频", "audio": "音频（自动决策）",
                 "subs": "字幕", "dur": "时长", "size": "大小", "action": "动作"}
        widths = {"status": 110, "progress": 80, "video": 210, "audio": 230,
                  "subs": 90, "dur": 70, "size": 85, "action": 95}
        wrap = ttk.Frame(main, style="Card.TFrame")
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings",
                                 selectmode="extended", style="Treeview")
        self.tree.heading("#0", text="文件名")
        self.tree.column("#0", width=300, minwidth=160)
        for c in cols:
            self.tree.heading(c, text=texts[c])
            self.tree.column(c, width=widths[c], minwidth=50,
                             anchor="center" if c in ("status", "progress", "dur", "size", "action") else "w")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure(ST_DONE, foreground=C_OK)
        self.tree.tag_configure(ST_FAIL, foreground=C_ERR)
        self.tree.tag_configure(ST_SKIP, foreground=C_DIM)
        self.tree.tag_configure(ST_RUNNING, foreground=C_ACCENT)
        self.tree.tag_configure(ST_ABORT, foreground=C_WARN)
        self.tree.bind("<Button-3>", self._popup_menu)
        self.tree.bind("<Double-1>", lambda e: self.show_cmd())

        # ---- 高级选项（默认隐藏）----
        self.adv = ttk.Frame(outer, style="Card.TFrame", padding=(16, 12))
        ttk.Label(self.adv, text="高级选项", style="Card.TLabel",
                  font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(self.adv, text="一般无需修改 · 改动会自动保存", style="CardDim.TLabel"
                  ).grid(row=0, column=3, sticky="e")
        for i in range(4):
            self.adv.columnconfigure(i, weight=1 if i else 0)

        r1 = ttk.Frame(self.adv, style="Card.TFrame"); r1.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 2))
        ttk.Label(r1, text="视频编码", style="CardDim.TLabel").pack(side="left")
        self.cmb_encoder = ttk.Combobox(r1, state="readonly", width=20, values=[
            "硬件 h264_amf（快·推荐）", "软件 libx264（画质最佳）"])
        self.cmb_encoder.current(0)
        self.cmb_encoder.pack(side="left", padx=(8, 20))
        self.cmb_encoder.bind("<<ComboboxSelected>>", lambda e: self._on_advanced_changed())
        self.var_qp_i = tk.IntVar(value=18)
        self.var_qp_p = tk.IntVar(value=20)
        self.var_crf = tk.IntVar(value=18)
        self.var_preset = tk.StringVar(value="medium")
        self.lbl_qp = ttk.Label(r1, text="质量 QP I/P", style="CardDim.TLabel")
        self._sp_qp_i = ttk.Spinbox(r1, from_=10, to=30, width=4, textvariable=self.var_qp_i, command=self._on_advanced_changed)
        self._sp_qp_p = ttk.Spinbox(r1, from_=10, to=34, width=4, textvariable=self.var_qp_p, command=self._on_advanced_changed)
        self.lbl_crf = ttk.Label(r1, text="CRF", style="CardDim.TLabel")
        self.sp_crf = ttk.Spinbox(r1, from_=12, to=26, width=4, textvariable=self.var_crf, command=self._on_advanced_changed)
        self.lbl_preset = ttk.Label(r1, text="预设", style="CardDim.TLabel")
        self.cmb_preset = ttk.Combobox(r1, textvariable=self.var_preset, state="readonly",
                                       width=9, values=X264_PRESETS)
        for sb in (self._sp_qp_i, self._sp_qp_p):
            sb.bind("<KeyRelease>", lambda e: self._on_advanced_changed())
        self.sp_crf.bind("<KeyRelease>", lambda e: self._on_advanced_changed())
        self.cmb_preset.bind("<<ComboboxSelected>>", lambda e: self._on_advanced_changed())

        r2 = ttk.Frame(self.adv, style="Card.TFrame"); r2.grid(row=2, column=0, columnspan=4, sticky="ew", pady=2)
        self.var_hwdec = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="硬件解码 (d3d11va)", style="Card.TCheckbutton",
                        variable=self.var_hwdec, command=self._on_advanced_changed).pack(side="left")
        self.var_hdr = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="HDR 转 SDR", style="Card.TCheckbutton",
                        variable=self.var_hdr, command=self._on_advanced_changed).pack(side="left", padx=(16, 0))
        ttk.Label(r2, text="色调映射", style="CardDim.TLabel").pack(side="left", padx=(16, 0))
        tm_values = [("★ %s" % a if a in TONEMAP_STARRED else a) for a in TONEMAP_ORDER]
        self.var_tm = tk.StringVar(value=tm_values[0])
        self._tm_values = tm_values
        self.cmb_tm = ttk.Combobox(r2, textvariable=self.var_tm, state="readonly", width=12,
                                   values=tm_values)
        self.cmb_tm.pack(side="left", padx=(8, 16))
        self.cmb_tm.bind("<<ComboboxSelected>>", lambda e: self._on_advanced_changed())
        self.var_skip = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="跳过已是目标格式的文件", style="Card.TCheckbutton",
                        variable=self.var_skip, command=self._on_advanced_changed).pack(side="left")

        r3 = ttk.Frame(self.adv, style="Card.TFrame"); r3.grid(row=3, column=0, columnspan=4, sticky="ew", pady=2)
        ttk.Label(r3, text="音频策略", style="CardDim.TLabel").pack(side="left")
        self.cmb_audio = ttk.Combobox(r3, state="readonly", width=34, values=[
            "自动（推荐）DTS/AC3 直通 · 其余转 AC3 5.1",
            "全部原样保留",
            "全部转 AC3 5.1 (640k)"])
        self.cmb_audio.current(0)
        self.cmb_audio.pack(side="left", padx=(8, 0))
        self.cmb_audio.bind("<<ComboboxSelected>>", lambda e: self._on_advanced_changed())
        ttk.Label(r3, text="（HDMI ARC 仅支持 DD / DTS / 2.0PCM 直通）",
                  style="Tiny.TLabel", background=C_CARD).pack(side="left", padx=(10, 0))

        r35 = ttk.Frame(self.adv, style="Card.TFrame"); r35.grid(row=4, column=0, columnspan=4, sticky="ew", pady=2)
        ttk.Label(r35, text="4K 片源", style="CardDim.TLabel").pack(side="left")
        self.cmb_uhd = ttk.Combobox(r35, state="readonly", width=32, values=[
            "智能（推荐）HEVC 保留 4K · 其他降 1080p",
            "一律转 1080p H.264"])
        self.cmb_uhd.current(0)
        self.cmb_uhd.pack(side="left", padx=(8, 0))
        self.cmb_uhd.bind("<<ComboboxSelected>>", lambda e: self._on_advanced_changed())
        ttk.Label(r35, text="（投影 HEVC 硬解 4K@60 · H.264 仅 4K@30）",
                  style="Tiny.TLabel", background=C_CARD).pack(side="left", padx=(10, 0))

        r4 = ttk.Frame(self.adv, style="Card.TFrame"); r4.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(2, 0))
        ttk.Label(r4, text="输出位置", style="CardDim.TLabel").pack(side="left")
        self.cmb_out = ttk.Combobox(r4, state="readonly", width=18, values=[
            "源目录下的子文件夹", "与源文件同目录", "自定义目录"])
        self.cmb_out.current(0)
        self.cmb_out.pack(side="left", padx=(8, 0))
        self.cmb_out.bind("<<ComboboxSelected>>", lambda e: self._on_advanced_changed())
        self.var_subfolder = tk.StringVar(value="FFY_输出")
        self.ent_sub = ttk.Entry(r4, textvariable=self.var_subfolder, width=12)
        self.var_outcustom = tk.StringVar(value="")
        self.ent_custom = ttk.Entry(r4, textvariable=self.var_outcustom, width=32)
        self.btn_custom = CuteButton(r4, text="浏览…", height=36, size=10, padx=18,
                                     bg=C_CARD, command=self.browse_out)
        self.var_overwrite = tk.BooleanVar(value=False)
        self.ck_over = ttk.Checkbutton(r4, text="覆盖已存在输出", style="Card.TCheckbutton",
                                       variable=self.var_overwrite, command=self._on_advanced_changed)
        CuteButton(r4, text="ffmpeg 路径…", height=36, size=10, padx=18, bg=C_CARD,
                   command=self.pick_ffmpeg).pack(side="right")

        # ---- 日志（收纳于高级选项内）----
        lf = ttk.Frame(self.adv, style="Card.TFrame")
        lf.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        lf.columnconfigure(0, weight=1)
        logbar = ttk.Frame(lf, style="Card.TFrame")
        logbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(logbar, text="运行日志", style="CardDim.TLabel",
                  font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        ttk.Label(logbar, text="完整日志同时保存在 FFY_logs 文件夹", style="Tiny.TLabel",
                  background=C_CARD).pack(side="left", padx=(10, 0))
        self.log = ScrolledText(lf, height=6, state="disabled", font=("Consolas", 9),
                                wrap="char", bg="#FAFBFD", fg=C_MUT, relief="flat",
                                insertbackground=C_TEXT, selectbackground="#DCE4FA")
        self.log.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.log.tag_config("ok", foreground=C_OK)
        self.log.tag_config("err", foreground=C_ERR)
        self.log.tag_config("dim", foreground=C_DIM)

        # ---- 底部控制条 ----
        bottom = ttk.Frame(outer, style="Card.TFrame", padding=(16, 12))
        bottom.grid(row=3, column=0, sticky="ew", padx=12, pady=(10, 12))
        bottom.columnconfigure(2, weight=1)
        self.btn_start = CuteButton(bottom, text="▶  开始转码", kind="primary",
                                    height=52, size=14, bg=C_CARD,
                                    command=self.start_batch)
        self.btn_start.grid(row=0, column=0)
        self.btn_stop = CuteButton(bottom, text="■  停止", kind="danger",
                                   height=52, size=13, bg=C_CARD,
                                   command=self.stop_batch)
        self.btn_stop.set_enabled(False)
        self.btn_stop.grid(row=0, column=1, padx=(12, 0))
        self.overall = ttk.Progressbar(bottom, mode="determinate", length=140)
        self.overall.grid(row=0, column=2, sticky="ew", padx=18)
        self.lbl_overall = ttk.Label(bottom, text="就绪 · 共 0 个文件", style="CardDim.TLabel")
        self.lbl_overall.grid(row=0, column=3)

        self.menu = tk.Menu(self.root, tearoff=0, bg=C_CARD, fg=C_TEXT,
                            activebackground=C_HOVER, activeforeground=C_TEXT,
                            relief="flat", bd=0)
        self.menu.add_command(label="查看转码命令", command=self.show_cmd)
        self.menu.add_command(label="重新检测", command=self.reprobe_selected)
        self.menu.add_command(label="重新排队", command=self.requeue_selected)
        self.menu.add_separator()
        self.menu.add_command(label="打开所在文件夹", command=self.open_src_folder)
        self.menu.add_command(label="打开输出文件夹", command=self.open_out_folder)
        self.menu.add_separator()
        self.menu.add_command(label="移除", command=self.remove_selected)

        self._sync_quality_widgets()
        self._sync_out_widgets()
        self._update_ff_label()

    # ------------------------------------------------------------ 高级面板
    def toggle_advanced(self):
        if self.adv.winfo_ismapped():
            self.adv.grid_remove()
            self.btn_adv.set_text("⚙ 高级选项")
        else:
            self.adv.grid(row=1, column=0, sticky="ew", padx=12, pady=(10, 0))
            self.btn_adv.set_text("⚙ 收起")

    def _on_advanced_changed(self, *_):
        self._sync_quality_widgets()
        self._sync_out_widgets()
        save_settings(self._read_cfg_from_ui())
        # 参数可能影响"动作/音频"列的显示，重刷新全部行
        for t in self.tasks.values():
            if t.info:
                self._refresh_row(t)
        self._update_overall()

    # ------------------------------------------------------------ 设置读写
    def _tm_key(self):
        v = self.var_tm.get()
        for a in TONEMAP_ORDER:
            if v.endswith(a):
                return a
        return TONEMAP_ORDER[0]

    def _read_cfg_from_ui(self):
        c = self.cfg
        c.encoder = "amf" if self.cmb_encoder.current() == 0 else "x264"
        try:
            c.qp_i, c.qp_p = int(self.var_qp_i.get()), int(self.var_qp_p.get())
        except tk.TclError:
            pass
        try:
            c.crf = int(self.var_crf.get())
        except tk.TclError:
            pass
        c.x264_preset = self.var_preset.get()
        c.hw_decode = bool(self.var_hwdec.get())
        c.hdr2sdr = bool(self.var_hdr.get())
        c.tonemap_algo = self._tm_key()
        c.audio_policy = ("auto", "copy_all", "ac3_all")[self.cmb_audio.current()]
        c.uhd_policy = ("smart", "h264_1080")[self.cmb_uhd.current()]
        c.skip_compliant = bool(self.var_skip.get())
        c.out_mode = ("subfolder", "same", "custom")[self.cmb_out.current()]
        c.out_subfolder = self.var_subfolder.get().strip() or "FFY_输出"
        c.out_custom = self.var_outcustom.get().strip()
        c.overwrite = bool(self.var_overwrite.get())
        return c

    def _load_cfg_to_ui(self):
        c = self.cfg
        self.cmb_encoder.current(0 if c.encoder == "amf" else 1)
        self.var_qp_i.set(c.qp_i)
        self.var_qp_p.set(c.qp_p)
        self.var_crf.set(c.crf)
        self.var_preset.set(c.x264_preset)
        self.var_hwdec.set(c.hw_decode)
        self.var_hdr.set(c.hdr2sdr)
        algo = c.tonemap_algo if c.tonemap_algo in TONEMAP_ORDER else "mobius"
        self.var_tm.set("★ %s" % algo if algo in TONEMAP_STARRED else algo)
        self.cmb_audio.current({"auto": 0, "copy_all": 1, "ac3_all": 2}[c.audio_policy])
        self.cmb_uhd.current(0 if c.uhd_policy == "smart" else 1)
        self.var_skip.set(c.skip_compliant)
        self.cmb_out.current({"subfolder": 0, "same": 1, "custom": 2}[c.out_mode])
        self.var_subfolder.set(c.out_subfolder)
        self.var_outcustom.set(c.out_custom)
        self.var_overwrite.set(c.overwrite)
        self._sync_quality_widgets()
        self._sync_out_widgets()

    def _sync_quality_widgets(self):
        amf = self.cmb_encoder.current() == 0
        for w in (self.lbl_qp, self._sp_qp_i, self._sp_qp_p,
                  self.lbl_crf, self.sp_crf, self.lbl_preset, self.cmb_preset):
            w.pack_forget()
        if amf:
            self.lbl_qp.pack(side="left")
            self._sp_qp_i.pack(side="left", padx=(6, 0))
            self._sp_qp_p.pack(side="left", padx=(4, 24))
        else:
            self.lbl_crf.pack(side="left")
            self.sp_crf.pack(side="left", padx=(6, 12))
            self.lbl_preset.pack(side="left", padx=(12, 0))
            self.cmb_preset.pack(side="left", padx=(6, 24))

    def _sync_out_widgets(self):
        idx = self.cmb_out.current()
        self.ent_sub.pack_forget(); self.btn_custom.pack_forget(); self.ent_custom.pack_forget()
        if idx == 0:
            self.ent_sub.pack(side="left", padx=(8, 16))
        elif idx == 2:
            self.ent_custom.pack(side="left", padx=(8, 0))
            self.btn_custom.pack(side="left", padx=(6, 16))
        self.ck_over.pack_forget()
        self.ck_over.pack(side="left", padx=(20, 0))

    def browse_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.var_outcustom.set(d)
            self._on_advanced_changed()

    def pick_ffmpeg(self):
        p = filedialog.askopenfilename(title="选择 ffmpeg.exe",
                                       filetypes=[("ffmpeg.exe", "ffmpeg.exe"), ("所有文件", "*.*")])
        if p:
            self.cfg.ffmpeg_path = p
            save_settings(self.cfg)
            self._update_ff_label()

    def _update_ff_label(self):
        ok = self.cfg.validate()
        self.lbl_ff.config(text=("● " + self.cfg.ffmpeg_path if ok
                                 else "● 未找到 ffmpeg，点击“高级选项”设置路径"),
                           foreground=(C_DIM if ok else C_ERR))

    # ------------------------------------------------------------ 列表管理
    def add_files(self):
        files = filedialog.askopenfilenames(title="选择影片文件", filetypes=[
            ("视频文件", "*.mkv *.mp4 *.m4v *.avi *.mov *.wmv *.flv *.ts *.m2ts *.mts *.webm *.mpg *.mpeg *.vob *.rmvb"),
            ("所有文件", "*.*")])
        for f in files:
            self.add_task(f)

    def add_folder(self):
        d = filedialog.askdirectory(title="选择文件夹（含子文件夹，将自动搜索影片）")
        if not d:
            return
        found = []
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames
                           if x not in ("$RECYCLE.BIN", "System Volume Information")
                           and not x.startswith("FFY_输出")]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in VIDEO_EXTS and not fn.startswith((".", "._")):
                    p = os.path.join(dirpath, fn)
                    try:
                        if os.path.getsize(p) >= 20 * 1024 * 1024:
                            found.append(p)
                    except OSError:
                        pass
        found.sort()
        if not found:
            self.log_line("扫描完成：未发现视频文件（%s）" % d, "dim")
            return
        # 统一输出目录：所选文件夹的同级位置，如 下载\视频 -> 下载\FFY_输出
        cfg = self._read_cfg_from_ui()
        if cfg.out_mode == "custom" and cfg.out_custom:
            out_dir = cfg.out_custom
        else:
            parent = os.path.dirname(os.path.abspath(d)) or os.path.abspath(d)
            out_dir = os.path.join(parent, cfg.out_subfolder or "FFY_输出")
        # 平铺后同名文件加后缀：(2) (3) …
        seen = {}
        dup = 0
        for p in found:
            stem = os.path.splitext(os.path.basename(p))[0].lower()
            n = seen.get(stem, 0) + 1
            seen[stem] = n
            if n > 1:
                dup += 1
            self.add_task(p, out_dir=out_dir,
                          out_suffix=(" (%d)" % n) if n > 1 else "")
        msg = "扫描完成：%s（%d 个视频文件）→ 统一输出到 %s" % (d, len(found), out_dir)
        if dup:
            msg += "（%d 个同名文件已自动加序号后缀）" % dup
        self.log_line(msg, "dim")

    def add_task(self, path, out_dir="", out_suffix=""):
        if any(t.path == path for t in self.tasks.values()):
            return
        self.iid_seq += 1
        iid = str(self.iid_seq)
        t = Task(iid=iid, path=path, out_dir=out_dir, out_suffix=out_suffix)
        self.tasks[iid] = t
        self.tree.insert("", "end", iid=iid, text=os.path.basename(path),
                         values=(ST_WAIT_PROBE, "", "…", "…", "…", "", "", ""))
        self._update_overall()
        self._probe_async(t)

    def remove_selected(self):
        for iid in self.tree.selection():
            if self.tasks.get(iid) and self.tasks[iid].status == ST_RUNNING:
                continue
            self.tree.delete(iid)
            self.tasks.pop(iid, None)
        self._update_overall()

    def clear_all(self):
        if any(t.status == ST_RUNNING for t in self.tasks.values()):
            messagebox.showwarning(APP_TITLE, "正在转码，请先停止。")
            return
        for iid in list(self.tasks):
            self.tree.delete(iid)
        self.tasks.clear()
        self._update_overall()

    def reprobe_selected(self):
        for iid in self.tree.selection():
            t = self.tasks.get(iid)
            if t and t.status != ST_RUNNING:
                t.reset()
                t.status = ST_WAIT_PROBE
                self._refresh_row(t)
                self._probe_async(t)

    def requeue_selected(self):
        for iid in self.tree.selection():
            t = self.tasks.get(iid)
            if t and t.status != ST_RUNNING:
                t.reset()
                self._refresh_row(t)
        self._update_overall()

    # ------------------------------------------------------------ 探测
    def _probe_async(self, t: Task):
        t.status = ST_PROBING
        self._refresh_row(t)
        cfg = self._read_cfg_from_ui()

        def worker():
            try:
                info = probe_file(cfg.ffmpeg_path, t.path)
                self.msg_q.put(("probe_ok", t.iid, info))
            except Exception as e:
                self.msg_q.put(("probe_fail", t.iid, str(e)))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ 批量转码
    def start_batch(self):
        cfg = self._read_cfg_from_ui()
        if not cfg.validate():
            messagebox.showerror(APP_TITLE, "未找到 ffmpeg：\n%s\n\n请在“高级选项”中设置 ffmpeg 路径。" % cfg.ffmpeg_path)
            return
        save_settings(cfg)
        if not self.tasks:
            messagebox.showinfo(APP_TITLE, "请先添加影片文件。")
            return
        if self.batch_running:
            return
        self.stop_flag = False
        self.batch_running = True
        self._open_log_file()
        self.btn_start.set_enabled(False)
        self.btn_stop.set_enabled(True)
        self.log_line("开始批量转码（%s · %s · 音频%s）" % (
            "硬编 h264_amf" if cfg.encoder == "amf" else "软编 libx264",
            "HDR→SDR " + cfg.tonemap_algo if cfg.hdr2sdr else "不转SDR",
            {"auto": "自动策略", "copy_all": "全部直通", "ac3_all": "全部转AC3"}[cfg.audio_policy]), "dim")
        self.enc_thread = threading.Thread(target=self._encode_worker, args=(cfg,), daemon=True)
        self.enc_thread.start()

    def stop_batch(self):
        if not self.batch_running:
            return
        self.stop_flag = True
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _next_task(self):
        for iid in self.tree.get_children(""):
            t = self.tasks.get(iid)
            if t and t.status == ST_READY and t.info:
                return t
        return None

    def _encode_worker(self, cfg: Settings):
        try:
            while not self.stop_flag:
                t = self._next_task()
                if not t:
                    if self._all_probed():
                        break
                    time.sleep(0.3)
                    continue
                if (cfg.skip_compliant and t.info["compliant"]
                        and audio_all_passthrough(t.info, cfg)):
                    t.status = ST_SKIP
                    t.note = "已达标"
                    self._post_row(t)
                    self._post_log("跳过（已是目标格式）：%s" % t.info["name"], "dim")
                    continue
                out = resolve_out_path(t.info, cfg, t)
                t.out_path = out
                if os.path.exists(out) and not cfg.overwrite:
                    t.status = ST_SKIP
                    t.note = "输出已存在"
                    self._post_row(t)
                    self._post_log("跳过（输出已存在）：%s" % out, "dim")
                    continue
                try:
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                except OSError as e:
                    t.status = ST_FAIL
                    t.note = "目录错误"
                    self._post_row(t)
                    self._post_log("失败（输出目录）：%s (%s)" % (out, e), "err")
                    continue

                t.cmd = build_cmd(t.info, cfg, out)
                t.status = ST_RUNNING
                t.progress = 0.0
                t.t_start = time.time()
                self._post_row(t)
                self._post_log("转码开始：%s" % t.info["name"])
                self._post_log("  " + subprocess.list2cmdline(t.cmd), "dim")
                code = self._run_ffmpeg(t)
                t.elapsed = time.time() - t.t_start
                if self.stop_flag and code != 0:
                    t.status = ST_ABORT
                    self._post_row(t)
                    self._post_log("已中止：%s" % t.info["name"], "err")
                    break
                if code == 0 and os.path.isfile(out):
                    try:
                        t.out_size = os.path.getsize(out)
                    except OSError:
                        pass
                    t.progress = 100.0
                    t.status = ST_DONE
                    self._post_row(t)
                    self._post_log("完成：%s  耗时 %s  输出 %s（原 %s）" % (
                        os.path.basename(out), fmt_duration(t.elapsed),
                        fmt_size(t.out_size), fmt_size(t.info["size"])), "ok")
                else:
                    t.status = ST_FAIL
                    tail = "\n".join(t.err_tail[-12:])
                    t.note = "退出码 %s" % code
                    self._post_row(t)
                    self._post_log("失败：%s（退出码 %s）\n%s" % (t.info["name"], code, tail), "err")
        finally:
            self.batch_running = False
            self.msg_q.put(("batch_done", None, None))

    def _all_probed(self):
        return all(t.info is not None or t.status in (ST_FAIL, ST_SKIP)
                   for t in self.tasks.values())

    def _run_ffmpeg(self, t: Task):
        try:
            self.proc = subprocess.Popen(
                t.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY)
        except OSError as e:
            t.err_tail = [str(e)]
            return -1
        proc = self.proc

        def read_err():
            for line in proc.stderr:
                line = line.strip()
                if line:
                    t.err_tail.append(line)
                    if len(t.err_tail) > 200:
                        del t.err_tail[:100]

        th = threading.Thread(target=read_err, daemon=True)
        th.start()

        duration = t.info["duration"] or 0
        block = {}
        last_emit = 0.0
        for line in proc.stdout:
            k, _, v = line.strip().partition("=")
            if not k:
                continue
            block[k] = v
            if k == "progress":
                sec = parse_out_time_sec(block)
                if sec is not None and duration > 0:
                    t.progress = min(100.0, sec / duration * 100.0)
                t.speed = block.get("speed", t.speed)
                if time.time() - last_emit >= 0.5 or block.get("progress") == "end":
                    last_emit = time.time()
                    self._post_row(t)
                block = {}
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        th.join(timeout=5)
        self.proc = None
        return proc.returncode

    # ------------------------------------------------------------ UI 事件泵
    def _poll(self):
        try:
            while True:
                kind, a, b = self.msg_q.get_nowait()
                if kind == "probe_ok":
                    t = self.tasks.get(a)
                    if t:
                        t.info = b
                        t.status = ST_READY
                        self._refresh_row(t)
                elif kind == "probe_fail":
                    t = self.tasks.get(a)
                    if t:
                        t.status = ST_FAIL
                        t.note = "检测失败"
                        self._refresh_row(t)
                        self.log_line("检测失败：%s\n  %s" % (t.path, b), "err")
                elif kind == "row":
                    t = self.tasks.get(a)
                    if t:
                        self._refresh_row(t)
                elif kind == "log":
                    self.log_line(a, b)
                elif kind == "batch_done":
                    self.btn_start.set_enabled(True)
                    self.btn_stop.set_enabled(False)
                    self._close_log_file()
                    if not self.stop_flag:
                        self.log_line("===== 全部任务结束 =====", "ok")
                        self.root.bell()
                    else:
                        self.log_line("===== 已停止 =====", "err")
                self._update_overall()
        except queue.Empty:
            pass
        self.root.after(250, self._poll)

    def _post_row(self, t: Task):
        self.msg_q.put(("row", t.iid, None))

    def _post_log(self, text, tag=None):
        self.msg_q.put(("log", text, tag))

    def _refresh_row(self, t: Task):
        cfg = self.cfg
        vals = {
            "status": t.status,
            "progress": ("%.0f%%" % t.progress) if t.status == ST_RUNNING else (
                "100%" if t.status == ST_DONE and t.progress >= 100 else ""),
            "video": video_desc(t.info),
            "audio": audio_desc(t.info, cfg),
            "subs": sub_desc(t.info),
            "dur": fmt_duration(t.info["duration"]) if t.info else "",
            "size": (fmt_size(t.out_size) if t.status == ST_DONE and t.out_size
                     else fmt_size(t.info["size"]) if t.info else ""),
            "action": (t.note or action_desc(t.info, cfg)) if t.status in (ST_SKIP, ST_FAIL)
                      else action_desc(t.info, cfg),
        }
        self.tree.item(t.iid, values=tuple(vals[c] for c in
                        ("status", "progress", "video", "audio", "subs", "dur", "size", "action")),
                       tags=(t.status,))

    def _update_overall(self):
        n = len(self.tasks)
        done = sum(1 for t in self.tasks.values() if t.status in (ST_DONE, ST_SKIP, ST_FAIL, ST_ABORT))
        running = next((t for t in self.tasks.values() if t.status == ST_RUNNING), None)
        pct = 0.0
        if n:
            pct = sum((100.0 if t.status in (ST_DONE, ST_SKIP) else t.progress) for t in self.tasks.values()) / n
        self.overall.config(value=pct, maximum=100)
        extra = ""
        if running and running.speed:
            rem = 0.0
            try:
                fac = float(running.speed.rstrip("x")) or 1.0
                for t in self.tasks.values():
                    if t.status in (ST_READY, ST_RUNNING) and t.info:
                        rem += t.info["duration"] * (1 - t.progress / 100.0)
                if rem > 0:
                    extra = " · 剩余约 %s" % fmt_duration(rem / fac)
            except (ValueError, ZeroDivisionError):
                pass
        self.lbl_overall.config(text=("%d/%d 完成%s" % (done, n, extra)) if n else "就绪 · 共 0 个文件")

    # ------------------------------------------------------------ 菜单/其他
    def _popup_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def show_cmd(self):
        for iid in self.tree.selection():
            t = self.tasks.get(iid)
            if not t:
                continue
            if not t.info:
                messagebox.showinfo(APP_TITLE, "该文件尚未检测完成。")
                return
            cfg = self._read_cfg_from_ui()
            cmd = build_cmd(t.info, cfg, resolve_out_path(t.info, cfg, t))
            win = tk.Toplevel(self.root, bg=C_BG)
            win.title("转码命令 - " + t.info["name"])
            win.geometry("920x400")
            txt = ScrolledText(win, font=("Consolas", 9), wrap="word", bg=C_FIELD,
                               fg=C_TEXT, relief="flat", insertbackground=C_TEXT)
            txt.pack(fill="both", expand=True, padx=10, pady=(10, 6))
            txt.insert("1.0", subprocess.list2cmdline(cmd))
            txt.config(state="disabled")

            def copy_cmd():
                self.root.clipboard_clear()
                self.root.clipboard_append(subprocess.list2cmdline(cmd))

            CuteButton(win, text="📋 复制到剪贴板", kind="primary", height=40,
                       size=11, bg=C_BG, command=copy_cmd).pack(pady=(2, 12))
            break

    def open_src_folder(self):
        for iid in self.tree.selection():
            t = self.tasks.get(iid)
            if t:
                os.startfile(os.path.dirname(t.path))   # noqa
                break

    def open_out_folder(self):
        for iid in self.tree.selection():
            t = self.tasks.get(iid)
            if t:
                d = (os.path.dirname(t.out_path) if t.out_path
                     else os.path.dirname(resolve_out_path(t.info, self._read_cfg_from_ui(), t) if t.info else t.path))
                if d:
                    os.startfile(d)   # noqa
                break

    # ------------------------------------------------------------ 日志
    def _open_log_file(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            self.log_file = open(os.path.join(LOG_DIR, time.strftime("FFY_%Y%m%d_%H%M%S.log")),
                                 "a", encoding="utf-8")
        except OSError:
            self.log_file = None

    def _close_log_file(self):
        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
            self.log_file = None

    def log_line(self, text, tag=None):
        ts = time.strftime("%H:%M:%S")
        self.log.config(state="normal")
        self.log.insert("end", "[%s] %s\n" % (ts, text), tag)
        if float(self.log.index("end-1c").split(".")[0]) > 3000:
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.config(state="disabled")
        if self.log_file:
            try:
                self.log_file.write("[%s] %s\n" % (ts, text))
                self.log_file.flush()
            except Exception:
                pass

    def _on_close(self):
        if self.batch_running:
            if not messagebox.askyesno(APP_TITLE, "正在转码，退出将中止当前任务。\n确定退出吗？"):
                return
            self.stop_flag = True
            if self.proc:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
        save_settings(self._read_cfg_from_ui())
        self._close_log_file()
        self.root.destroy()


# ----------------------------------------------------------------------------
# 命令行模式（自测/高级用途）
# ----------------------------------------------------------------------------
def cli_main(argv):
    if argv[1] == "probe" and len(argv) >= 3:
        cfg = load_settings()
        info = probe_file(cfg.ffmpeg_path, argv[2])
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if argv[1] == "cmd" and len(argv) >= 3:
        cfg = load_settings()
        for extra in argv[3:]:
            if extra == "--encoder=x264":
                cfg.encoder = "x264"
            elif extra == "--encoder=amf":
                cfg.encoder = "amf"
            elif extra == "--no-hdr":
                cfg.hdr2sdr = False
            elif extra == "--audio=copy":
                cfg.audio_policy = "copy_all"
            elif extra == "--audio=ac3":
                cfg.audio_policy = "ac3_all"
            elif extra == "--sw-decode":
                cfg.hw_decode = False
        info = probe_file(cfg.ffmpeg_path, argv[2])
        out = resolve_out_path(info, cfg)
        cmd = build_cmd(info, cfg, out)
        print("输出: %s" % out)
        print(subprocess.list2cmdline(cmd))
        return 0
    return None


def main():
    argv = sys.argv
    if len(argv) >= 2 and argv[1] in ("probe", "cmd"):
        sys.exit(cli_main(argv) or 0)

    root = tk.Tk()
    app = App(root)
    if len(argv) >= 2 and argv[1] == "smoke":
        root.after(2500, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
