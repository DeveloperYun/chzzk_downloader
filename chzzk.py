#!/usr/bin/env python3
"""
치지직(Chzzk) 다시보기(VOD) URL에서 스트림을 내려받는 스크립트.

- 재생: api.chzzk.naver.com/service/v3/videos + m3u8(DASH) 구조 (yt-dlp chzzk extractor와 동일)
- 19+·연령 제한: 브라우저에서 본인인증 후, 로그인 쿠키(NID_SES, NID_AUT 등)를
  넣어 API와 스트림 요청이 모두 그 세션을 쓰도록 할 것.

  예: Netscape 쿠키 파일(--cookies) 또는 --cookie "NID_SES=...; NID_AUT=..."
  환경 변수 CHZZK_COOKIE 도 동일 형식으로 지정 가능합니다.

- GUI: 인자 없이 실행, 또는 `python chzzk.py --gui` / `-g` (URL·쿠키·저장 폴더·일시정지·진행률·완료 알림)
  URL 칸에 **한 줄에 하나**로 여러 개 넣으면 **위→아래 순(큐)으로 연속** 다운로드.
  개별 URL이 실패해도 **다음 URL은 계속** 시도하며, 실패한 항목은 **`저장 폴더/chzzk_failed.log`** 에 탭 구분으로 누적.
  기본 저장 파일명: **`[YYYY_MM_DD]_제목.mp4`** (`publishDateAt` 기준, 없으면 오늘 날짜).
- **화질(세로)**: GUI·`--quality`로 **0(가용 최대)·2160~360 등** 상한을 고르고, ffprobe로 manifest 안에서 그 **이하**인 변형+오디오를 골라 `-c copy`로 MP4(기본 1080p). **용량을 더 줄이려면** 「재인코딩」 또는 `--reencode`.
- ffprobe 는 ffmpeg 설치에 함께 옵니다.

왜 ffmpeg? 다시보기는 브라우저에 보이는 주소가 “한 통짜 mp4 링크”가 아니라 m3u8/MPD 등으로
여러 조각(세그먼트)으로 전송됩니다. URL만 알면 그 조각들을 받아 한 파일로 합치는 도구가 필요하고,
이 스크립트는 그 역할에 ffmpeg를 사용합니다. (터미널에서 `ffmpeg -version`이 나오면 준비된 것.)

- **Ubuntu/Debian(소스 실행):** `sudo apt update && sudo apt install -y python3-tk ffmpeg`
  (`python3-tk` = GUI용 tkinter, `ffmpeg` = 스트림 합치기). 무화면 서버는 GUI 대신
  `python3 chzzk.py 'https://…' -o 저장.mp4` 처럼 CLI만 사용하세요.
- **Windows(소스 실행):** ffmpeg 설치 후 PATH. 예: `winget install Gyan.FFmpeg` 또는
  https://www.gyan.dev/ffmpeg/builds/ 의 `bin`을 PATH에 추가. 새 터미널에서 `ffmpeg -version` 확인.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except ImportError:  # pragma: no cover
    tk = None  # type: ignore[assignment,misc]


class ChzzkError(Exception):
    pass


def _subprocess_win_hide_console() -> dict[str, Any]:
    """Windows에서 ffmpeg를 띄울 때 콘솔(검은 창)이 잠깐 뜨는 것을 막는다 (GUI용)."""
    if sys.platform != "win32":
        return {}
    c = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": c} if c else {}


def _ffmpeg_missing_message() -> str:
    if sys.platform == "win32":
        return (
            "ffmpeg 를 찾을 수 없습니다.\n\n"
            "[Windows] 설치 예:\n"
            "  · PowerShell:  winget install Gyan.FFmpeg\n"
            "  · 또는 gyan.dev 에서 full 빌드 zip → 압축 해제 후 `bin` 폴더를\n"
            "    ‘환경 변수’의 PATH(사용자)에 넣기\n\n"
            "설정 후 **새로 연** 터미널/창에서 `ffmpeg -version` 이 되는지 확인하세요."
        )
    return "ffmpeg 를 찾을 수 없습니다. 시스템에 설치한 뒤 PATH에 등록하세요."


def _default_downloads_dir() -> str:
    if sys.platform == "win32":
        home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    else:
        home = os.path.expanduser("~")
    cand = os.path.join(home, "Downloads")
    return cand if os.path.isdir(cand) else home


def _utf8_console_if_windows() -> None:
    if sys.platform != "win32":
        return
    for s in (sys.stdout, sys.stderr):
        reconf = getattr(s, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except (OSError, ValueError, TypeError):
            pass

CHZZK_API = "https://api.chzzk.naver.com/service/v3/videos/{}"
PLAYBACK = "https://apis.naver.com/neonplayer/vodplay/v1/playback/{}"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# target_h: 0 = manifest에서 가용한 최대 세로(해상도), >0 = 해당 p 이하·가장 가까운 변형(없으면 그보다 큰 최저)
DEFAULT_TARGET_HEIGHT = 1080
# 하위호환(내부 기본)
TARGET_VIDEO_HEIGHT = DEFAULT_TARGET_HEIGHT

VIDEO_RE = re.compile(
    r"https?://(?:(?:m|www)\.)?chzzk\.naver\.com/video/(?P<id>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)

# (세로 p, Combobox 표시)
QUALITY_GUI_OPTIONS: tuple[tuple[int, str], ...] = (
    (0, "최고 (가용 최대)"),
    (2160, "2160p 4K (가용 시)"),
    (1440, "1440p (가용 시)"),
    (1080, "1080p Full HD (기본)"),
    (720, "720p"),
    (480, "480p"),
    (360, "360p"),
)


def _height_from_gui_quality_label(label: str) -> int:
    for h, lab in QUALITY_GUI_OPTIONS:
        if lab == label:
            return h
    return DEFAULT_TARGET_HEIGHT


def _log_download_failure(log_dir: str, url: str, err: str) -> None:
    """
    배치 다운로드에서 한 URL이 실패했을 때 log_dir에 chzzk_failed.log 로 한 줄씩 누적.
    (다음 URL 다운로드는 이미 루프에서 계속됨)
    """
    if not log_dir or not os.path.isdir(log_dir):
        return
    path = os.path.join(log_dir, "chzzk_failed.log")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    one = (err or "").replace("\n", " ").replace("\r", " ").strip()[:2000]
    line = f"{ts}\t{url}\t{one}\n"
    try:
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
    except OSError:
        pass


def _parse_url_list(text: str) -> list[str]:
    """
    한 줄에 하나(또는 # 주석) 다시보기 URL. 앞뒤 공백·빈 줄 제거.
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _parse_netscape_cookie_file(path: str) -> str:
    """Load Mozilla/Netscape format cookies; return a single Cookie header value."""
    pairs: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            name, value = parts[5], parts[6]
            pairs.append(f"{name}={value}")
    if not pairs:
        raise ChzzkError("쿠키 파일에서 항목을 읽지 못했습니다.")
    return "; ".join(pairs)


def _resolve_cookie_arg(cookie_file: str | None, cookie: str | None) -> str:
    if cookie_file and cookie:
        raise ChzzkError("--cookies 와 --cookie 는 동시에 쓰지 마세요.")
    if cookie_file:
        return _parse_netscape_cookie_file(cookie_file)
    if cookie:
        return cookie.strip()
    return (os.environ.get("CHZZK_COOKIE") or "").strip()


def _normalize_cookie_header(s: str) -> str:
    """
    HTTP Cookie 값에는 CR/LF가 올 수 없다(httplib/urllib ValueError).
    GUI 등에서 쿠키를 여러 줄로 붙여넣은 경우 `name=값` 조각을 `; `로 잇는다.
    """
    if not s or not str(s).strip():
        return ""
    pieces: list[str] = []
    for block in str(s).replace("\r", "\n").split("\n"):
        block = block.strip()
        if not block:
            continue
        for p in block.split(";"):
            p = p.strip()
            if p and "=" in p:
                pieces.append(p)
    return "; ".join(pieces)


def _api_request(
    video_id: str, cookie_header: str, extra_headers: dict[str, str] | None = None
) -> dict[str, Any]:
    url = CHZZK_API.format(urllib.parse.quote(video_id, safe=""))
    h = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://chzzk.naver.com/",
    }
    if extra_headers:
        h.update(extra_headers)
    if cookie_header:
        h["Cookie"] = _normalize_cookie_header(cookie_header)
    req = urllib.request.Request(url, headers=h, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        t = e.read().decode("utf-8", errors="replace")
        raise ChzzkError(f"API HTTP {e.code}: {t[:500]}") from e
    except urllib.error.URLError as e:
        raise ChzzkError(f"API 연결 실패: {e}") from e
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ChzzkError("API 응답 형식이 올바르지 않습니다.")
    code = data.get("code")
    if code not in (200, 0, None) and "content" not in data:
        raise ChzzkError(
            f"API 오류: {data.get('message', data)}"[:800]
        )
    content = data.get("content")
    if not content:
        msg = (data.get("message") or "") + str(data)
        hint = ""
        if "19" in msg or "성인" in msg or "연령" in msg or not msg:
            hint = (
                " (연령 제한/로그인이 필요할 수 있습니다. --cookie 또는 --cookies 로 "
                "로그인·본인인증된 세션 쿠키를 넣어 보세요.)"
            )
        raise ChzzkError(f"비디오 메타를 가져올 수 없습니다.{hint}\n{msg[:500]}")
    return content


def _stream_url_from_content(content: dict[str, Any]) -> tuple[str, str]:
    """
    Return (url, kind) where kind is 'm3u8' or 'mpd' for display/logging.
    """
    status = content.get("vodStatus")
    vid = content.get("videoId")
    in_key = content.get("inKey")

    if status == "ABR_HLS" and vid and in_key:
        q = urllib.parse.urlencode(
            {
                "key": in_key,
                "env": "real",
                "lc": "ko_KR",
                "cpl": "ko_KR",
            }
        )
        u = f"{PLAYBACK.format(vid)}?{q}"
        return u, "mpd"

    lrp = content.get("liveRewindPlaybackJson")
    if isinstance(lrp, str):
        try:
            playback = json.loads(lrp)
        except json.JSONDecodeError as e:
            raise ChzzkError(f"liveRewindPlaybackJson 파싱 실패: {e}") from e
    elif isinstance(lrp, dict):
        playback = lrp
    else:
        playback = None

    if not playback:
        raise ChzzkError(
            "재생 정보(liveRewindPlaybackJson)가 없습니다. "
            "19금·로그인 필요 영상이면 --cookie / --cookies 로 네이버 로그인 쿠키를 지정하세요."
        )
    media = playback.get("media")
    if not media:
        raise ChzzkError("재생 media 항목이 없습니다. 인코딩 대기(UPLOAD) 등일 수 있습니다.")
    path = media[0].get("path")
    if not path:
        raise ChzzkError("스트림 path가 비어 있습니다.")
    u = str(path)
    if ".m3u8" in u.lower() or "m3u8" in u.lower():
        return u, "m3u8"
    return u, "hls-url"


def _ffmpeg_headers(cookie_header: str) -> str:
    lines = [
        f"User-Agent: {UA}",
        "Accept: */*",
        "Origin: https://chzzk.naver.com",
        "Referer: https://chzzk.naver.com/",
    ]
    if cookie_header:
        c = _normalize_cookie_header(cookie_header)
        if c:
            lines.append(f"Cookie: {c}")
    return "\r\n".join(lines) + "\r\n"


def _ffprobe_map_args_for_target_height(
    input_url: str, headers: str, target_h: int
) -> tuple[list[str], str]:
    """
    ffprobe로 비디오·오디오 인덱스를 찾아 -map 0:… 인자로 반환.
    target_h==0: 가용 비디오 중 세로가 가장 큰 것.
    target_h>0: 정확 일치·없으면 target_h 이하 최대, 모두 그보다 크면 가장 낮은 것(스트림).
    """
    if not shutil.which("ffprobe"):
        goal = "최고" if not target_h else f"≤{target_h}p"
        return (
            [],
            f"ffprobe 없음(자동 스트림) — {goal} 선호(ffmpeg·ffprobe 같이 설치 권장)",
        )
    wn: dict[str, Any] = _subprocess_win_hide_console()
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-probesize",
                "32M",
                "-analyzeduration",
                "20M",
                "-headers",
                headers,
                "-i",
                input_url,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            **wn,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return [], f"ffprobe 실패(건너뜀): {e!s}"[:220]
    if r.returncode != 0:
        return [], f"ffprobe 오류(건너뜀): {(r.stderr or '')[:200]}"
    try:
        j: dict[str, Any] = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return [], "ffprobe JSON 파싱 실패(건너뜀)"
    streams: list[dict[str, Any]] = list(j.get("streams") or [])
    vids: list[dict[str, Any]] = []
    auds: list[dict[str, Any]] = []
    for s in streams:
        ct = s.get("codec_type")
        if ct == "video":
            vids.append(s)
        elif ct == "audio":
            auds.append(s)
    if not vids:
        return [], "비디오 스트림 없음(건너뜀)"

    def h(s: dict[str, Any]) -> int:
        try:
            return int(s.get("height") or 0)
        except (TypeError, ValueError):
            return 0

    cands = [s for s in vids if h(s) > 0]
    if not cands:
        chosen: dict[str, Any] = vids[0]
    else:
        if not target_h:
            chosen = max(cands, key=lambda s: h(s))
        else:
            exact = [s for s in cands if h(s) == target_h]
            if exact:
                chosen = exact[0]
            else:
                under = [s for s in cands if h(s) <= target_h]
                if under:
                    chosen = max(under, key=lambda s: h(s))
                else:
                    chosen = min(cands, key=lambda s: h(s))
    hi = h(chosen) or int(str(chosen.get("height") or 0) or 0)
    try:
        wi = int(chosen.get("width") or 0)
    except (TypeError, ValueError):
        wi = 0
    aidx: int | None = None
    if auds:
        try:
            aidx = int(auds[0].get("index", -1))
        except (TypeError, ValueError):
            aidx = None
        if aidx is not None and aidx < 0:
            aidx = None
    vix: int | None
    try:
        vix = int(chosen["index"])
    except (TypeError, ValueError, KeyError):
        return [], "비디오 인덱스 없음(건너뜀)"
    args: list[str] = ["-map", f"0:{vix}"]
    if aidx is not None:
        args += ["-map", f"0:{aidx}"]
    if not target_h:
        if hi and wi:
            label = f"{wi}x{hi} (최고 화질)"
        else:
            label = f"stream 0:{vix} (최고)"
    elif hi == target_h and wi and hi:
        label = f"{wi}x{hi} ({target_h}p)"
    elif hi:
        label = f"{wi}x{hi} (가용, 목표 ≤{target_h}p)"
    else:
        label = f"stream 0:{vix} (목표 ≤{target_h}p)"
    return args, label


def _ffmpeg_set_paused(proc: subprocess.Popen, pause: bool) -> None:
    """psutil(가능 시) 또는 Linux SIGSTOP/SIGCONT으로 ffmpeg를 일시정지/재개."""
    if proc.poll() is not None:
        return
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            p = psutil.Process(proc.pid)
            if pause:
                p.suspend()
            else:
                p.resume()
            return
        except Exception as e:
            raise ChzzkError(f"일시정지/재개 실패: {e}") from e
    if (
        sys.platform != "win32"
        and getattr(signal, "SIGSTOP", None) is not None
        and getattr(signal, "SIGCONT", None) is not None
    ):
        try:
            if pause:
                os.kill(proc.pid, signal.SIGSTOP)
            else:
                os.kill(proc.pid, signal.SIGCONT)
        except (ProcessLookupError, OSError) as e:
            raise ChzzkError(f"일시정지/재개 실패: {e}") from e
        return
    raise ChzzkError(
        "이 환경에서는 일시정지를 지원하지 못합니다. `pip install psutil` 을 설치하거나, "
        "지원 Linux에서 SIGSTOP 를 사용하세요."
    )


_FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")


def _ffmpeg_time_to_seconds(m: re.Match[str]) -> float:
    h, mm, s = m.group(1), m.group(2), m.group(3)
    return int(h) * 3600 + int(mm) * 60 + float(s)


def _default_reencode_crf(vcodec: str) -> int:
    v = (vcodec or "h264").lower()
    return 28 if v in ("hevc", "h265", "x265") else 24


def _build_reencode_ffmpeg_args(
    *,
    reencode: bool,
    reencode_vcodec: str,
    reencode_crf: int | None,
    reencode_preset: str,
) -> list[str]:
    if not reencode:
        return ["-c", "copy"]
    v = (reencode_vcodec or "h264").lower()
    if v in ("hevc", "h265", "x265"):
        crf = reencode_crf if reencode_crf is not None else _default_reencode_crf("hevc")
        return [
            "-c:v",
            "libx265",
            "-crf",
            str(crf),
            "-preset",
            reencode_preset,
            "-pix_fmt",
            "yuv420p",
            "-tag:v",
            "hvc1",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ]
    crf = reencode_crf if reencode_crf is not None else _default_reencode_crf("h264")
    return [
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        reencode_preset,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
    ]


def _reencode_status_note(
    reencode: bool, reencode_vcodec: str, reencode_crf: int | None, reencode_preset: str
) -> str:
    if not reencode:
        return ""
    v = (reencode_vcodec or "h264").lower()
    crf = reencode_crf if reencode_crf is not None else _default_reencode_crf(
        "hevc" if v in ("hevc", "h265", "x265") else "h264"
    )
    enc = "HEVC" if v in ("hevc", "h265", "x265") else "H.264"
    return f"재인코딩 {enc} CRF{crf} {reencode_preset}"


def _run_ffmpeg(
    input_url: str,
    output_path: str,
    cookie_header: str,
    print_cmd: bool,
    *,
    reencode: bool = False,
    reencode_vcodec: str = "h264",
    reencode_crf: int | None = None,
    reencode_preset: str = "medium",
    target_height: int = DEFAULT_TARGET_HEIGHT,
    on_progress: Callable[[float | None, str | None], None] | None = None,
    duration_sec: float | None = None,
    on_ffmpeg_start: Callable[[Any], None] | None = None,
) -> None:
    if not shutil.which("ffmpeg"):
        raise ChzzkError(_ffmpeg_missing_message())
    headers = _ffmpeg_headers(cookie_header)
    th = int(target_height) if target_height is not None else DEFAULT_TARGET_HEIGHT
    if th < 0:
        th = DEFAULT_TARGET_HEIGHT
    map_args, qnote = _ffprobe_map_args_for_target_height(input_url, headers, th)
    renote = _reencode_status_note(
        reencode, reencode_vcodec, reencode_crf, reencode_preset
    )
    if reencode and renote:
        qnote = f"{qnote} | {renote}" if qnote else renote
    enc_args = _build_reencode_ffmpeg_args(
        reencode=reencode,
        reencode_vcodec=reencode_vcodec,
        reencode_crf=reencode_crf,
        reencode_preset=reencode_preset,
    )
    if not on_progress:
        print(f"화질: {qnote}", file=sys.stderr)
    loglevel = "info"
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-loglevel",
        loglevel,
        "-headers",
        headers,
        "-i",
        input_url,
        *map_args,
        *enc_args,
        "-f",
        "mp4",
        "-movflags",
        "+faststart",
        output_path,
    ]
    if print_cmd:
        hlen = len(headers)
        print(
            f"[ff] headers bytes={hlen}, input URL(scheme+host hidden)=...",
            file=sys.stderr,
        )
    win: dict[str, Any] = _subprocess_win_hide_console()

    def _emit(frac: float | None, msg: str) -> None:
        if on_progress is None:
            return
        if qnote and msg:
            line = f"{qnote}  |  {msg}"
        elif qnote:
            line = qnote
        else:
            line = msg
        on_progress(frac, line)

    if on_progress is None:
        try:
            subprocess.run(cmd, check=True, **win)
        except subprocess.CalledProcessError as e:
            raise ChzzkError(f"ffmpeg 실패(종료 {e.returncode})") from e
        return
    try:
        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            **win,
        )
    except OSError as e:
        raise ChzzkError(f"ffmpeg 실행 실패: {e}") from e
    if on_ffmpeg_start is not None:
        on_ffmpeg_start(proc)
    if proc.stderr is None:
        raise ChzzkError("ffmpeg stderr 파이프를 열 수 없습니다.")
    dtot = float(duration_sec) if duration_sec and duration_sec > 0 else 0.0
    _emit(
        None,
        "다운로드·인코딩 중…" if reencode else "다운로드 중(스트림)…",
    )
    for line in proc.stderr:
        line = line.rstrip()
        m = _FFMPEG_TIME_RE.search(line)
        if m and dtot > 0:
            tsec = _ffmpeg_time_to_seconds(m)
            _emit(
                min(0.999, max(0.0, tsec / dtot)),
                f"{_format_hms(tsec)} / {_format_hms(dtot)}",
            )
        elif m and dtot <= 0:
            tsec = _ffmpeg_time_to_seconds(m)
            _emit(None, f"진행: {_format_hms(tsec)} (총 길이는 API에 없음)")
    rc = proc.wait()
    if rc != 0:
        raise ChzzkError(f"ffmpeg 실패(종료 {rc})")
    if on_progress:
        _emit(1.0, "인코딩·저장 완료" if reencode else "복사·mux 완료 (MP4)")


def _format_hms(seconds: float) -> str:
    s = int(round(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _duration_from_content(content: dict[str, Any]) -> float | None:
    d = content.get("duration")
    if d is None:
        return None
    try:
        v = float(d)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _date_prefix_from_content(content: dict[str, Any] | None) -> str:
    """저장 파일명 앞: [YYYY_MM_DD]_ (API publishDateAt, 없으면 로컬 오늘)."""
    dt = datetime.now().astimezone()
    if content:
        raw = content.get("publishDateAt")
        if raw is not None:
            try:
                f = float(raw)
                if f > 1e12:  # ms
                    f /= 1000.0
                dt = datetime.fromtimestamp(f, tz=timezone.utc).astimezone()
            except (OSError, ValueError, OverflowError, TypeError):
                pass
    return f"[{dt.year:04d}_{dt.month:02d}_{dt.day:02d}]_"


def _default_out_path(
    video_id: str, title: str | None, content: dict[str, Any] | None = None
) -> str:
    pre = _date_prefix_from_content(content)
    body = re.sub(r"[^\w가-힣.-]+", "_", (title or f"chzzk_{video_id}"))[:120]
    body = body.strip("._") or f"chzzk_{video_id}"
    safe = (f"{pre}{body}")[:200]
    return f"{safe}.mp4"


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def _cli_resolved_path(
    video_id: str,
    title: str | None,
    out_arg: str | None,
    nurls: int,
    content: dict[str, Any] | None = None,
) -> str:
    """CLI -o: URL 1개 → 파일·폴더 모두 가능. 2개 이상 → -o 는 폴더(또는 생략=현재 디렉터리)."""
    fn = _default_out_path(video_id, title, content)
    if nurls == 1:
        if not out_arg:
            p = fn if fn.lower().endswith(".mp4") else f"{fn}.mp4"
            return _unique_path(p)
        o = os.path.normpath(out_arg)
        if os.path.isdir(o):
            return _unique_path(os.path.join(o, os.path.basename(fn)))
        p = o if o.lower().endswith(".mp4") else f"{o}.mp4"
        return _unique_path(p)
    if out_arg and not os.path.isdir(out_arg):
        raise ChzzkError(
            "URL이 둘 이상일 때는 -o(또는 --output)에 저장 '폴더'만 지정하세요. "
            f" ({out_arg!r} 은 폴더가 아닙니다.)"
        )
    base = os.path.normpath(out_arg) if out_arg else os.getcwd()
    return _unique_path(os.path.join(base, os.path.basename(fn)))


def _run_gui() -> None:
    _utf8_console_if_windows()
    if tk is None:
        print("tkinter 를 사용할 수 없습니다.", file=sys.stderr)
        sys.exit(1)
    default_dir = _default_downloads_dir()

    root = tk.Tk()
    root.title("치지직 VOD 다운로드")
    root.minsize(520, 420)
    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(0, weight=1)

    frm = ttk.Frame(root, padding=10)
    frm.grid(row=0, column=0, sticky="nsew")
    frm.grid_columnconfigure(1, weight=1)

    ttk.Label(frm, text="다시보기 URL(줄마다 1개, 위→아래 순)").grid(
        row=0, column=0, sticky="nw", pady=(0, 4)
    )
    txt_url = scrolledtext.ScrolledText(frm, height=5, width=64, font=(None, 9))
    txt_url.grid(row=0, column=1, sticky="nsew", pady=(0, 4))
    frm.rowconfigure(0, weight=1)

    lf_cookie = ttk.LabelFrame(
        frm,
        text="쿠키 (선택) — 연령 제한 VOD",
        padding=8,
    )
    lf_cookie.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(4, 8))
    lf_cookie.grid_columnconfigure(0, weight=1)
    lf_cookie.grid_rowconfigure(2, weight=1)
    frm.rowconfigure(1, weight=1)
    ttk.Label(
        lf_cookie,
        text=(
            "연령 제한/로그인이 필요한 영상은 NID_SES=…; NID_AUT=… 처럼 "
            "브라우저에서 복사한 쿠키를 아래에 붙여 넣으세요. "
            "필요 없으면 비워 둡니다."
        ),
        font=(None, 9),
        wraplength=500,
    ).grid(row=0, column=0, sticky="w", pady=(0, 4))
    row_cookie_btn = ttk.Frame(lf_cookie)
    row_cookie_btn.grid(row=1, column=0, sticky="ew", pady=(0, 4))
    btn_cookie_file = ttk.Button(row_cookie_btn, text="Netscape 쿠키 파일에서 불러오기…")
    btn_cookie_file.pack(side=tk.LEFT)
    txt_cook = scrolledtext.ScrolledText(lf_cookie, height=5, width=68, font=(None, 9))
    txt_cook.grid(row=2, column=0, sticky="nsew")

    def load_cookie_file() -> None:
        p = filedialog.askopenfilename(
            title="쿠키 파일 (브라우저 확장에서 내보낸 Netscape 형식)",
            filetypes=[("텍스트", "*.txt"), ("모든 파일", "*")],
        )
        if not p:
            return
        try:
            s = _parse_netscape_cookie_file(p)
        except ChzzkError as e:
            messagebox.showerror("쿠키", str(e))
            return
        except OSError as e:
            messagebox.showerror("쿠키", str(e))
            return
        txt_cook.delete("1.0", tk.END)
        txt_cook.insert("1.0", s)

    btn_cookie_file.configure(command=load_cookie_file)

    dir_frame = ttk.Frame(frm)
    dir_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
    dir_frame.grid_columnconfigure(1, weight=1)
    ttk.Label(dir_frame, text="저장 폴더").grid(row=0, column=0, padx=(0, 6))
    ent_dir = ttk.Entry(dir_frame)
    ent_dir.insert(0, default_dir)
    ent_dir.grid(row=0, column=1, sticky="ew")
    btn_browse = ttk.Button(dir_frame, text="찾아보기…")
    btn_browse.grid(row=0, column=2, padx=(6, 0))

    qual_frame = ttk.Frame(frm)
    qual_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 6))
    qual_frame.grid_columnconfigure(1, weight=1)
    ttk.Label(qual_frame, text="화질(세로·상한)").grid(row=0, column=0, padx=(0, 8))
    _qlabels = [lab for _, lab in QUALITY_GUI_OPTIONS]
    var_quality = tk.StringVar(
        value=next(l for h, l in QUALITY_GUI_OPTIONS if h == DEFAULT_TARGET_HEIGHT)
    )
    cb_quality = ttk.Combobox(
        qual_frame,
        textvariable=var_quality,
        state="readonly",
        width=42,
        values=_qlabels,
    )
    cb_quality.grid(row=0, column=1, sticky="w")
    ttk.Label(
        qual_frame,
        text="manifest에 있는 변형 중 위에 맞는 것(없으면 가장 가까운 쪽).",
        font=(None, 8),
        wraplength=520,
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

    enc_frame = ttk.LabelFrame(frm, text="용량(선택)", padding=6)
    enc_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 6))
    var_reencode = tk.BooleanVar(value=False)
    var_reencode_vcodec = tk.StringVar(value="h264")
    ttk.Checkbutton(
        enc_frame,
        text="재인코딩(파일이 작아지고, 걸리는 시간·CPU는 늘어남, 화질은 손실)",
        variable=var_reencode,
    ).pack(anchor="w")
    row_enc2 = ttk.Frame(enc_frame)
    row_enc2.pack(fill=tk.X, pady=(4, 0))
    ttk.Label(row_enc2, text="비디오:").pack(side=tk.LEFT, padx=(0, 6))
    cb_reenc_v = ttk.Combobox(
        row_enc2,
        textvariable=var_reencode_vcodec,
        state="readonly",
        width=18,
        values=("h264", "hevc"),
    )
    cb_reenc_v.pack(side=tk.LEFT)
    ttk.Label(
        enc_frame,
        text="(기본 H.264 CRF 24, AAC 128k / HEVC는 용량 더↓·인코딩 더 느림, ffmpeg에 libx265 필요)",
        font=(None, 8),
        wraplength=520,
    ).pack(anchor="w", pady=(4, 0))

    pbar = ttk.Progressbar(frm, mode="determinate", length=400, maximum=100)
    pbar.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 4))
    lbl_status = ttk.Label(frm, text="대기 중", font=(None, 9))
    lbl_status.grid(row=6, column=0, columnspan=2, sticky="w")

    fr_btns = ttk.Frame(frm)
    fr_btns.grid(row=7, column=0, columnspan=2, pady=(10, 0))
    btn_go = ttk.Button(fr_btns, text="다운로드", width=16)
    btn_go.pack(side=tk.LEFT, padx=(0, 6))
    btn_pause = ttk.Button(fr_btns, text="일시정지", width=10, state=tk.DISABLED)
    btn_pause.pack(side=tk.LEFT)

    ff_state: dict[str, Any] = {"proc": None, "paused": False}

    def browse_dir() -> None:
        d = filedialog.askdirectory(
            initialdir=ent_dir.get().strip() or default_dir,
            title="다운로드 폴더 선택",
        )
        if d:
            ent_dir.delete(0, tk.END)
            ent_dir.insert(0, d)

    def stop_indeterminate() -> None:
        try:
            pbar.stop()
        except tk.TclError:
            pass
        pbar["mode"] = "determinate"

    def on_batch_done(
        success: list[str],
        failed: list[tuple[str, str]],
        log_dir: str | None = None,
    ) -> None:
        stop_indeterminate()
        pbar["value"] = 100.0 if success else 0.0
        lbl_status.configure(
            text=f"완료 {len(success)}개, 실패 {len(failed)}개" if failed else "완료"
        )
        btn_go.configure(state=tk.NORMAL)
        btn_pause.configure(state=tk.DISABLED, text="일시정지")
        log_note = (
            f"\n\n실패 URL·사유는 로그에 누적했습니다:\n{os.path.join(log_dir, 'chzzk_failed.log')}"
            if (failed and log_dir)
            else ""
        )
        if failed and not success:
            body = "\n".join(f"· {u}\n  {em[:200]}" for u, em in failed[:8])
            if len(failed) > 8:
                body += f"\n… 외 {len(failed) - 8}건"
            messagebox.showerror("다운로드", f"모두 실패했습니다.\n\n{body}{log_note}")
        elif failed:
            ok_lines = "\n".join(success[:6])
            if len(success) > 6:
                ok_lines += f"\n… 외 {len(success) - 6}개"
            fail_body = "\n".join(f"· {u[:60]}…\n  {em[:120]}" for u, em in failed[:5])
            if len(failed) > 5:
                fail_body += f"\n… 외 {len(failed) - 5}건"
            messagebox.showwarning(
                "다운로드 일부 실패",
                f"성공 {len(success)}개, 실패 {len(failed)}개 (다음 URL은 계속 진행)\n\n"
                f"[저장됨]\n{ok_lines}\n\n[실패]\n{fail_body}{log_note}",
            )
        else:
            lines = "\n".join(success[:10])
            if len(success) > 10:
                lines += f"\n… 외 {len(success) - 10}개"
            t = f"{len(success)}개 모두 저장했습니다." if len(success) > 1 else "저장 완료."
            messagebox.showinfo("다운로드 완료", f"{t}\n\n{lines}")

    def on_done_err(err: str) -> None:
        stop_indeterminate()
        pbar["value"] = 0.0
        lbl_status.configure(text="오류")
        btn_go.configure(state=tk.NORMAL)
        btn_pause.configure(state=tk.DISABLED, text="일시정지")
        messagebox.showerror("오류", err)

    def _detach_ffmpeg() -> None:
        ff_state["proc"] = None
        ff_state["paused"] = False
        btn_pause.configure(state=tk.DISABLED, text="일시정지")

    def toggle_pause() -> None:
        proc = ff_state.get("proc")
        if not proc or proc.poll() is not None:
            return
        try:
            if not ff_state.get("paused"):
                _ffmpeg_set_paused(proc, True)
                ff_state["paused"] = True
                btn_pause.configure(text="재개")
                lbl_status.configure(text="일시정지 — 재개를 누르면 이어서 받습니다")
            else:
                _ffmpeg_set_paused(proc, False)
                ff_state["paused"] = False
                btn_pause.configure(text="일시정지")
                lbl_status.configure(text="이어서 받는 중…")
        except ChzzkError as e:
            messagebox.showerror("일시정지", str(e))

    def work(reenc: bool, reenc_vcodec: str, target_h: int) -> None:
        try:
            urls = _parse_url_list(txt_url.get("1.0", tk.END))
            folder = ent_dir.get().strip()
            cookie_line = txt_cook.get("1.0", tk.END).strip()
            if not urls:
                root.after(0, lambda: on_done_err("다시보기 URL을 한 줄에 하나씩 입력하세요."))
                return
            if not folder or not os.path.isdir(folder):
                root.after(0, lambda: on_done_err("유효한 다운로드 폴더를 선택하세요."))
                return
            bad = [u for u in urls if not VIDEO_RE.search(u)]
            if bad:
                root.after(
                    0,
                    lambda: on_done_err(
                        "URL 형식이 올바르지 않은 줄이 있습니다.\n"
                        + "https://chzzk.naver.com/video/숫자\n\n"
                        + "\n".join(bad[:5])
                        + (f"\n… 외 {len(bad) - 5}줄" if len(bad) > 5 else "")
                    ),
                )
                return

            n = len(urls)
            success: list[str] = []
            failed: list[tuple[str, str]] = []

            for i, url in enumerate(urls):
                k = i + 1
                m = VIDEO_RE.search(url)
                if not m:
                    em = "URL 인식 실패(내부 오류)"
                    _log_download_failure(folder, url, em)
                    failed.append((url, em))
                    continue
                video_id = m.group("id")
                ev = threading.Event()

                def init_pbar(
                    dur: float | None,
                    ki: int,
                    ni: int,
                    use_reenc: bool,
                    th: int,
                ) -> None:
                    if dur and dur > 0:
                        pbar.configure(mode="determinate", value=0.0, maximum=100.0)
                    else:
                        pbar.configure(mode="indeterminate")
                        pbar.start(8)
                    phase = "재인코딩" if use_reenc else "다운로드(스트림·MP4)"
                    goal = "최고" if th <= 0 else f"≤{th}p"
                    lbl_status.configure(
                        text=f"[{ki}/{ni}] {phase} 중(목표 {goal})…"
                    )
                    ev.set()

                try:
                    content = _api_request(video_id, cookie_line)
                except ChzzkError as e:
                    em = str(e)
                    _log_download_failure(folder, url, em)
                    failed.append((url, em))
                    continue
                dur = _duration_from_content(content)
                title = (content.get("videoTitle") or "").strip() or None
                fname = _default_out_path(video_id, title, content)
                out_path = _unique_path(os.path.join(folder, fname))
                try:
                    stream_url, _ = _stream_url_from_content(content)
                except ChzzkError as e:
                    em = str(e)
                    _log_download_failure(folder, url, em)
                    failed.append((url, em))
                    continue
                root.after(
                    0,
                    lambda d=dur, ki=k, ni=n, ur=reenc, th=target_h: init_pbar(
                        d, ki, ni, ur, th
                    ),
                )
                if not ev.wait(timeout=10.0):
                    em = "초기화 시간 초과."
                    _log_download_failure(folder, url, em)
                    failed.append((url, em))
                    continue

                def on_ff(
                    frac: float | None,
                    msg: str | None,
                    *,
                    k: int = k,
                    n: int = n,
                ) -> None:
                    def _u() -> None:
                        if ff_state.get("paused"):
                            return
                        if frac is not None:
                            try:
                                pbar.stop()
                            except tk.TclError:
                                pass
                            pbar["mode"] = "determinate"
                            g = min(1.0, max(0.0, float(frac)))
                            overall = 100.0 * ((k - 1) + g) / n
                            pbar["value"] = min(100.0, max(0.0, overall))
                        if msg:
                            lbl_status.configure(text=f"[{k}/{n}] {msg}")

                    root.after(0, _u)

                def on_start_proc(p: Any) -> None:
                    def _bind() -> None:
                        ff_state["proc"] = p
                        ff_state["paused"] = False
                        btn_pause.configure(state=tk.NORMAL, text="일시정지")

                    root.after(0, _bind)

                try:
                    _run_ffmpeg(
                        stream_url,
                        out_path,
                        cookie_line,
                        False,
                        reencode=reenc,
                        reencode_vcodec=reenc_vcodec,
                        target_height=target_h,
                        on_progress=on_ff,
                        duration_sec=dur,
                        on_ffmpeg_start=on_start_proc,
                    )
                    success.append(out_path)
                except ChzzkError as e:
                    em = str(e)
                    _log_download_failure(folder, url, em)
                    failed.append((url, em))
            root.after(
                0,
                lambda s=success, f=failed, fd=folder: on_batch_done(s, f, fd),
            )
        except ChzzkError as e:
            root.after(0, lambda s=str(e): on_done_err(s))
        finally:
            root.after(0, _detach_ffmpeg)

    def start() -> None:
        try:
            pbar.stop()
        except tk.TclError:
            pass
        ff_state["proc"] = None
        ff_state["paused"] = False
        btn_go.configure(state=tk.DISABLED)
        btn_pause.configure(state=tk.DISABLED, text="일시정지")
        lbl_status.configure(text="정보를 불러오는 중…")
        pbar["mode"] = "determinate"
        pbar["value"] = 0.0
        reenc = var_reencode.get()
        reenc_v = (var_reencode_vcodec.get() or "h264").strip().lower()
        if reenc_v not in ("h264", "hevc"):
            reenc_v = "h264"
        target_h = _height_from_gui_quality_label(var_quality.get() or "")
        threading.Thread(
            target=work, args=(reenc, reenc_v, target_h), daemon=True
        ).start()

    btn_browse.configure(command=browse_dir)
    btn_go.configure(command=start)
    btn_pause.configure(command=toggle_pause)
    txt_url.focus_set()
    root.mainloop()


def _parse_quality_cli(s: str) -> int:
    t = (s or "").strip().lower().rstrip("p")
    if t in ("best", "max", "highest", "최고", "최상", "0"):
        return 0
    n = int(t, 10)
    if n < 0:
        raise argparse.ArgumentTypeError("화질(세로)은 0 이상이어야 합니다.")
    if n == 0:
        return 0
    if n < 64 or n > 7680:
        raise argparse.ArgumentTypeError(
            "화질(세로)은 0(최고) 또는 64~7680(예: 1080)이어야 합니다."
        )
    return n


def main() -> None:
    _utf8_console_if_windows()
    p = argparse.ArgumentParser(
        description="치지직 다시보기 VOD 를 ffmpeg로 내려받습니다 (HLS/MPD). "
        "인자 없이 실행하면 GUI가 열립니다.",
    )
    p.add_argument(
        "url",
        nargs="+",
        help="https://chzzk.naver.com/video/<id> (여러 개면 순서대로). 한 개만 쓰면 -o에 파일도 가능.",
    )
    p.add_argument(
        "-o",
        "--output",
        help="출력: URL 1개일 때 .mp4 파일 경로 가능. 여러 URL이면 이 값은 '저장 폴더'여야 합니다.",
    )
    p.add_argument(
        "--cookie",
        metavar="STR",
        help=(
            "요청에 붙일 Cookie (예: NID_SES=...; NID_AUT=...). 19+·연령 제한 시 필요. "
            "미지정이면 CHZZK_COOKIE 사용."
        ),
    )
    p.add_argument(
        "--cookies",
        metavar="FILE",
        help="Netscape/Mozilla 쿠키 파일 (브라우저 확장으로 내보낸 파일)",
    )
    p.add_argument(
        "--print-json",
        action="store_true",
        help="메타 API 원본(민감할 수 있음)을 stderr에 출력",
    )
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="스크림 URL만 표시하고 ffmpeg는 실행하지 않음",
    )
    p.add_argument(
        "--quality",
        type=_parse_quality_cli,
        default=DEFAULT_TARGET_HEIGHT,
        metavar="H",
        help=(
            "비디오 세로 해상도(상한, ffprobe·manifest). "
            "0=가용 최대, 1080,720,480,360,best(=0) 등. 기본 1080"
        ),
    )
    p.add_argument(
        "--reencode",
        action="store_true",
        help="재인코딩(용량↓·시간↑). H.264 또는 HEVC + AAC(기본 H.264 CRF 24, medium)",
    )
    p.add_argument(
        "--reencode-vcodec",
        choices=("h264", "hevc"),
        default="h264",
        metavar="K",
        help="재인코딩 비디오 코덱(기본 h264; hevc는 ffmpeg에 libx265 필요, 더 느림)",
    )
    p.add_argument(
        "--reencode-crf",
        type=int,
        default=None,
        metavar="N",
        help="CRF(낮을수록 화질↑·용량↑). h264 기본 24, hevc 기본 28(미지정 시)",
    )
    p.add_argument(
        "--reencode-preset",
        default="medium",
        help="x264/x265 preset(기본 medium; ultrafast~veryslow)",
    )
    args = p.parse_args()

    try:
        urls = [u.strip() for u in args.url if u.strip()]
        nurls = len(urls)
        if not urls:
            raise ChzzkError("URL이 없습니다.")
        bad = [u for u in urls if not VIDEO_RE.search(u)]
        if bad:
            raise ChzzkError(
                "지원하지 않는 URL이 있습니다.\n" + "\n".join(bad[:10])
            )
        cookie_header = _resolve_cookie_arg(args.cookies, args.cookie)
        if nurls > 1:
            log_dir = (
                args.output
                if (args.output and os.path.isdir(args.output))
                else os.getcwd()
            )
        else:
            if args.output:
                o = os.path.normpath(args.output)
                if os.path.isdir(o):
                    log_dir = o
                else:
                    ad = os.path.dirname(os.path.abspath(o))
                    log_dir = ad if ad else os.getcwd()
            else:
                log_dir = os.getcwd()
        ok_paths: list[str] = []
        err: list[tuple[str, str]] = []
        for url in urls:
            m = VIDEO_RE.search(url)
            if not m:
                em = "URL 인식 실패(parse)"
                if not args.dry_run:
                    _log_download_failure(log_dir, url, em)
                err.append((url, em))
                continue
            video_id = m.group("id")
            try:
                content = _api_request(video_id, cookie_header)
                if args.print_json:
                    print(
                        json.dumps(content, ensure_ascii=False, indent=2),
                        file=sys.stderr,
                    )
                title = (content.get("videoTitle") or "").strip() or None
                out = _cli_resolved_path(
                    video_id, title, args.output, nurls, content
                )
                stream_url, kind = _stream_url_from_content(content)
                if args.dry_run:
                    print("video_id:", video_id)
                    print("type:", kind)
                    print("stream_url:", stream_url)
                    print("output:", out)
                    print("quality (target height):", args.quality)
                    continue
                print(
                    f"다운로드({nurls}중): {title or video_id} -> {out}",
                    file=sys.stderr,
                )
                _run_ffmpeg(
                    stream_url,
                    out,
                    cookie_header,
                    print_cmd=False,
                    reencode=bool(args.reencode),
                    reencode_vcodec=args.reencode_vcodec,
                    reencode_crf=args.reencode_crf,
                    reencode_preset=args.reencode_preset,
                    target_height=int(args.quality),
                )
                ok_paths.append(out)
            except ChzzkError as e:
                em = str(e)
                if not args.dry_run:
                    _log_download_failure(log_dir, url, em)
                err.append((url, em))
        if not args.dry_run:
            for p in ok_paths:
                print(p)
        if not args.dry_run and err:
            for u, m in err:
                print(f"실패: {u} :: {m}", file=sys.stderr)
            print(
                f"실패 {len(err)}건 → 로그: {os.path.join(log_dir, 'chzzk_failed.log')}",
                file=sys.stderr,
            )
        if not args.dry_run and not ok_paths and err:
            sys.exit(1)
    except ChzzkError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def _wants_gui() -> bool:
    if len(sys.argv) == 1:
        return True
    return bool(len(sys.argv) == 2 and sys.argv[1] in ("--gui", "-g"))


if __name__ == "__main__":
    if _wants_gui():
        _run_gui()
    else:
        main()
