#!/usr/bin/env python3
"""
치지직(Chzzk) 다시보기(VOD) URL에서 스트림을 내려받는 스크립트.

- 재생: api.chzzk.naver.com/service/v3/videos + m3u8(DASH) 구조 (yt-dlp chzzk extractor와 동일)
- 19+·연령 제한: 브라우저에서 본인인증 후, 로그인 쿠키(NID_SES, NID_AUT 등)를
  넣어 API와 스트림 요청이 모두 그 세션을 쓰도록 할 것.

  예: Netscape 쿠키 파일(--cookies) 또는 --cookie "NID_SES=...; NID_AUT=..."
  환경 변수 CHZZK_COOKIE 도 동일 형식으로 지정 가능합니다.

- GUI: 인자 없이 실행, 또는 `python chzzk.py --gui` / `-g` (URL·쿠키·저장 폴더·진행률·완료 알림)

왜 ffmpeg? 다시보기는 브라우저에 보이는 주소가 “한 통짜 mp4 링크”가 아니라 m3u8/MPD 등으로
여러 조각(세그먼트)으로 전송됩니다. URL만 알면 그 조각들을 받아 한 파일로 합치는 도구가 필요하고,
이 스크립트는 그 역할에 ffmpeg를 사용합니다. (패키지 설치 후 터미널에서 `ffmpeg -version` 이
나오면 준비된 것입니다.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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

CHZZK_API = "https://api.chzzk.naver.com/service/v3/videos/{}"
PLAYBACK = "https://apis.naver.com/neonplayer/vodplay/v1/playback/{}"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

VIDEO_RE = re.compile(
    r"https?://(?:(?:m|www)\.)?chzzk\.naver\.com/video/(?P<id>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)


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
        h["Cookie"] = cookie_header
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
        lines.append(f"Cookie: {cookie_header}")
    return "\r\n".join(lines) + "\r\n"


_FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")


def _ffmpeg_time_to_seconds(m: re.Match[str]) -> float:
    h, mm, s = m.group(1), m.group(2), m.group(3)
    return int(h) * 3600 + int(mm) * 60 + float(s)


def _run_ffmpeg(
    input_url: str,
    output_path: str,
    cookie_header: str,
    print_cmd: bool,
    *,
    on_progress: Callable[[float | None, str | None], None] | None = None,
    duration_sec: float | None = None,
) -> None:
    if not shutil.which("ffmpeg"):
        raise ChzzkError("ffmpeg 를 찾을 수 없습니다. 시스템에 설치한 뒤 PATH에 등록하세요.")
    headers = _ffmpeg_headers(cookie_header)
    loglevel = "info" if on_progress else "info"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-loglevel",
        loglevel,
        "-headers",
        headers,
        "-i",
        input_url,
        "-c",
        "copy",
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
    if on_progress is None:
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            raise ChzzkError(f"ffmpeg 실패(종료 {e.returncode})") from e
        return
    try:
        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
        )
    except OSError as e:
        raise ChzzkError(f"ffmpeg 실행 실패: {e}") from e
    if proc.stderr is None:
        raise ChzzkError("ffmpeg stderr 파이프를 열 수 없습니다.")
    dtot = float(duration_sec) if duration_sec and duration_sec > 0 else 0.0
    for line in proc.stderr:
        line = line.rstrip()
        m = _FFMPEG_TIME_RE.search(line)
        if m and dtot > 0:
            tsec = _ffmpeg_time_to_seconds(m)
            on_progress(
                min(0.999, max(0.0, tsec / dtot)),
                f"{_format_hms(tsec)} / {_format_hms(dtot)}",
            )
        elif on_progress and m and dtot <= 0:
            tsec = _ffmpeg_time_to_seconds(m)
            on_progress(None, f"진행: {_format_hms(tsec)} (총 길이는 API에 없음)")
    rc = proc.wait()
    if rc != 0:
        raise ChzzkError(f"ffmpeg 실패(종료 {rc})")
    if on_progress:
        on_progress(1.0, "인코딩/복사 완료")


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


def _default_out_path(video_id: str, title: str | None) -> str:
    safe = re.sub(r"[^\w가-힣.-]+", "_", (title or f"chzzk_{video_id}"))[:120]
    safe = safe.strip("._") or f"chzzk_{video_id}"
    return f"{safe}.mp4"


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def _run_gui() -> None:
    if tk is None:
        print("tkinter 를 사용할 수 없습니다.", file=sys.stderr)
        sys.exit(1)
    default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
    if not os.path.isdir(default_dir):
        default_dir = os.path.expanduser("~")

    root = tk.Tk()
    root.title("치지직 VOD 다운로드")
    root.minsize(520, 420)
    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(0, weight=1)

    frm = ttk.Frame(root, padding=10)
    frm.grid(row=0, column=0, sticky="nsew")
    frm.grid_columnconfigure(1, weight=1)

    ttk.Label(frm, text="다시보기 URL").grid(row=0, column=0, sticky="nw", pady=(0, 4))
    ent_url = ttk.Entry(frm, width=60)
    ent_url.grid(row=0, column=1, sticky="ew", pady=(0, 4))

    lf_cookie = ttk.LabelFrame(
        frm,
        text="쿠키 (선택) — 19금·연령 제한 VOD",
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

    pbar = ttk.Progressbar(frm, mode="determinate", length=400, maximum=100)
    pbar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 4))
    lbl_status = ttk.Label(frm, text="대기 중", font=(None, 9))
    lbl_status.grid(row=4, column=0, columnspan=2, sticky="w")

    btn_go = ttk.Button(frm, text="다운로드", width=18)
    btn_go.grid(row=5, column=0, columnspan=2, pady=(10, 0))

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

    def on_done_ok(path: str) -> None:
        stop_indeterminate()
        pbar["value"] = 100.0
        lbl_status.configure(text="완료")
        btn_go.configure(state=tk.NORMAL)
        messagebox.showinfo("다운로드 완료", f"다운로드가 완료되었습니다.\n\n{path}")

    def on_done_err(err: str) -> None:
        stop_indeterminate()
        pbar["value"] = 0.0
        lbl_status.configure(text="오류")
        btn_go.configure(state=tk.NORMAL)
        messagebox.showerror("오류", err)

    def work() -> None:
        url = ent_url.get().strip()
        folder = ent_dir.get().strip()
        cookie_line = txt_cook.get("1.0", tk.END).strip()
        if not url:
            root.after(0, lambda: on_done_err("다시보기 URL을 입력하세요."))
            return
        if not folder or not os.path.isdir(folder):
            root.after(0, lambda: on_done_err("유효한 다운로드 폴더를 선택하세요."))
            return
        m = VIDEO_RE.search(url)
        if not m:
            root.after(0, lambda: on_done_err("URL 형식: https://chzzk.naver.com/video/숫자"))
            return
        video_id = m.group("id")
        ev = threading.Event()

        def init_pbar(dur: float | None) -> None:
            if dur and dur > 0:
                pbar.configure(mode="determinate", value=0.0, maximum=100.0)
            else:
                pbar.configure(mode="indeterminate")
                pbar.start(8)
            lbl_status.configure(text="다운로드 중…")
            ev.set()

        try:
            content = _api_request(video_id, cookie_line)
        except ChzzkError as e:
            root.after(0, lambda s=str(e): on_done_err(s))
            return
        dur = _duration_from_content(content)
        title = (content.get("videoTitle") or "").strip() or None
        fname = _default_out_path(video_id, title)
        out_path = _unique_path(os.path.join(folder, fname))
        try:
            stream_url, _ = _stream_url_from_content(content)
        except ChzzkError as e:
            root.after(0, lambda s=str(e): on_done_err(s))
            return
        root.after(0, lambda: init_pbar(dur))
        if not ev.wait(timeout=10.0):
            root.after(0, lambda: on_done_err("초기화 시간 초과."))
            return

        def on_ff(frac: float | None, msg: str | None) -> None:
            def _u() -> None:
                if frac is not None:
                    try:
                        pbar.stop()
                    except tk.TclError:
                        pass
                    pbar["mode"] = "determinate"
                    v = min(100.0, max(0.0, 100.0 * float(frac)))
                    pbar["value"] = v
                if msg:
                    lbl_status.configure(text=msg)

            root.after(0, _u)

        try:
            _run_ffmpeg(
                stream_url,
                out_path,
                cookie_line,
                False,
                on_progress=on_ff,
                duration_sec=dur,
            )
        except ChzzkError as e:
            root.after(0, lambda s=str(e): on_done_err(s))
            return
        root.after(0, lambda p=out_path: on_done_ok(p))

    def start() -> None:
        try:
            pbar.stop()
        except tk.TclError:
            pass
        btn_go.configure(state=tk.DISABLED)
        lbl_status.configure(text="정보를 불러오는 중…")
        pbar["mode"] = "determinate"
        pbar["value"] = 0.0
        threading.Thread(target=work, daemon=True).start()

    btn_browse.configure(command=browse_dir)
    btn_go.configure(command=start)
    ent_url.focus_set()
    root.mainloop()


def main() -> None:
    p = argparse.ArgumentParser(
        description="치지직 다시보기 VOD 를 ffmpeg로 내려받습니다 (HLS/MPD). "
        "인자 없이 실행하면 GUI가 열립니다.",
    )
    p.add_argument(
        "url",
        help="https://chzzk.naver.com/video/<id> 형식",
    )
    p.add_argument(
        "-o",
        "--output",
        help="출력 mp4 경로(미지정 시 제목 기반 파일명)",
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
    args = p.parse_args()

    try:
        m = VIDEO_RE.search(args.url.strip())
        if not m:
            raise ChzzkError(
                "chzzk 다시보기 URL만 지원합니다. 예: https://chzzk.naver.com/video/1234567"
            )
        video_id = m.group("id")
        cookie_header = _resolve_cookie_arg(args.cookies, args.cookie)

        content = _api_request(video_id, cookie_header)
        if args.print_json:
            print(json.dumps(content, ensure_ascii=False, indent=2), file=sys.stderr)

        title = (content.get("videoTitle") or "").strip() or None
        out = args.output
        if not out:
            out = _default_out_path(video_id, title)
        if not out.lower().endswith(".mp4"):
            out = out + ".mp4"

        stream_url, kind = _stream_url_from_content(content)
        if args.dry_run:
            print("video_id:", video_id)
            print("type:", kind)
            print("stream_url:", stream_url)
            print("output:", out)
            return

        print(f"다운로드: {title or video_id} -> {out}", file=sys.stderr)
        _run_ffmpeg(stream_url, out, cookie_header, print_cmd=False)
        print(out)
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
