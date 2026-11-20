# downloader.py
# Small helper to run yt-dlp per-file with isolated progress state,
# Windows-safe filenames, atomic writes, and lossless FFmpeg container remuxing.
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from urllib.parse import urlsplit

import requests
import yt_dlp
from tqdm import tqdm
from yt_dlp.utils import DownloadError

from models import Lesson
from urls import DeepLearningAIURLs

WINDOWS_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
VALID_VIDEO_CONTAINERS = ("mp4", "mkv", "source")


def sanitize_filename(name: str, max_length: int = 120) -> str:
    """
    Sanitize a filename component so it is valid on Windows, Linux, and macOS.

    - Replaces Windows-invalid characters (< > : " / \\ | ? * and control chars)
    - Preserves normal Unicode characters
    - Strips trailing spaces and periods
    - Prefixes Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
    - Limits length to avoid MAX_PATH issues on Windows
    """
    if not name or not name.strip():
        return "untitled"

    cleaned = WINDOWS_INVALID_CHARS.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = cleaned.rstrip(" .")

    if not cleaned:
        cleaned = "untitled"

    base_upper = cleaned.split(".")[0].upper()
    if base_upper in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")

    return cleaned or "untitled"


def is_ffmpeg_available() -> bool:
    """Return True if ffmpeg executable is available in PATH."""
    return shutil.which("ffmpeg") is not None


def detect_media_container(file_path: Path) -> str:
    """
    Inspect the first bytes of a local media file to identify its real container.
    Returns one of: 'mp4', 'mpegts', 'mkv', 'webm', 'unknown'.
    """
    if not file_path.exists() or file_path.stat().st_size == 0:
        return "unknown"

    with open(file_path, "rb") as f:
        header = f.read(564)

    # ISO Base Media File Format (MP4 / M4V / MOV): 'ftyp' at bytes 4..8
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return "mp4"

    # EBML header (Matroska / WebM): 0x1A 0x45 0xDF 0xA3
    if len(header) >= 4 and header[:4] == b"\x1a\x45\xdf\xa3":
        if b"webm" in header[:64].lower():
            return "webm"
        return "mkv"

    # MPEG-TS packets: sync byte 0x47 every 188 bytes
    if len(header) >= 377 and header[0] == 0x47 and header[188] == 0x47 and header[376] == 0x47:
        return "mpegts"
    if len(header) >= 189 and header[0] == 0x47 and header[188] == 0x47:
        return "mpegts"

    return "unknown"


def _redact_url(url: str) -> str:
    """Strip query string and fragment so signed media tokens are never logged."""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"
    except Exception:
        return "<redacted-url>"


class YTDLDownloader:
    def __init__(
        self,
        lesson_data: Lesson,
        out_dir: Path,
        session: requests.Session,
        position: int = 1,  # terminal row for this bar (0 reserved for outer)
        use_aria2: bool = False,
        aria2_args: Optional[Sequence[str]] = None,
        api_base_url: str = DeepLearningAIURLs.API_BASE_URL,
        video_container: str = "mp4",
        retry_count: int = 3,
        overwrite: bool = False,
        verbose: bool = False,
        file_stem_override: Optional[str] = None,
    ):
        """
        Initialize YTDLDownloader.
        """
        if video_container not in VALID_VIDEO_CONTAINERS:
            raise ValueError(
                f"Unsupported video_container '{video_container}'. "
                f"Expected one of: {', '.join(VALID_VIDEO_CONTAINERS)}"
            )

        self.lesson_data = lesson_data
        self.out_dir = out_dir
        self.file_stem = file_stem_override or f"{lesson_data.index:02d}_{sanitize_filename(lesson_data.name)}"
        self.api_base_url = api_base_url
        self.position = position
        self.use_aria2 = use_aria2
        self.video_container = video_container
        self.retry_count = max(0, retry_count)
        self.overwrite = overwrite
        self.verbose = verbose
        self.last_error: Optional[str] = None
        self.aria2_args = aria2_args or [
            "-x",
            "16",
            "-s",
            "16",
            "-j",
            "16",
            "-k",
            "1M",
            "--summary-interval=0",
        ]

        # progress bar kept per instance
        self._pbar: Optional[tqdm] = None

        self.session = session
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _progress_hook(self, d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)

            if self._pbar is None:
                filename = d.get(
                    "filename") or f"{self.file_stem}.{self.video_container}"
                short_name = Path(filename).stem[:60]
                self._pbar = tqdm(
                    total=total,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1000,
                    desc=short_name,
                    leave=False,
                    position=self.position,
                    dynamic_ncols=True,
                )

            if total:
                self._pbar.total = total
            self._pbar.n = downloaded
            self._pbar.refresh()

        elif status == "finished":
            if self._pbar is not None:
                if self._pbar.total and self._pbar.n < self._pbar.total:
                    self._pbar.n = self._pbar.total
                self._pbar.close()
                self._pbar = None

        elif status == "error":
            if self._pbar is not None:
                self._pbar.close()
                self._pbar = None

    def _build_opts(self) -> Dict[str, Any]:
        """Build options for yt-dlp, including explicit FFmpegVideoRemuxer when requested."""
        output_file_path = self.out_dir / f"{self.file_stem}.%(ext)s"

        opts: Dict[str, Any] = {
            "outtmpl": str(output_file_path.resolve()),
            "quiet": not self.verbose,
            "noprogress": True,
            "progress_hooks": [self._progress_hook],
            "no_warnings": not self.verbose,
            "logtostderr": False,
            "ratelimit": None,
            "throttledratelimit": None,
            "retries": max(1, self.retry_count) if self.retry_count else 0,
            "fragment_retries": max(3, self.retry_count * 2) if self.retry_count else 0,
            "retries": max(1, self.retry_count) if self.retry_count else 0,
            "fragment_retries": max(3, self.retry_count * 2) if self.retry_count else 0,
            "skip_unavailable_fragments": False,
            "continuedl": True,
            "overwrites": self.overwrite,
            "concurrent_fragment_downloads": 4,
        }

        if self.video_container in ("mp4", "mkv"):
            # merge_output_format alone does NOT remux single HLS MPEG-TS streams.
            # FFmpegVideoRemuxer losslessly remuxes MPEG-TS into an actual MP4/MKV container.
            opts["merge_output_format"] = self.video_container
            opts["postprocessors"] = [
                {
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": self.video_container,
                }
            ]

        if self.use_aria2:
            opts.update(
                {
                    "external_downloader": "aria2c",
                    "external_downloader_args": self.aria2_args,
                }
            )

        return opts

    def _save_file(
        self,
        url: str,
        save_file_path: Path,
        show_progress: bool = True,
        is_binary: bool = True,
    ) -> None:
        """Download a file atomically via a temporary .part file."""
        if save_file_path.exists() and save_file_path.stat().st_size > 0 and not self.overwrite:
            return

        mode = "wb" if is_binary else "w"
        save_file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = save_file_path.with_name(f"{save_file_path.name}.part")

        max_attempts = max(1, self.retry_count + 1)
        last_exc: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.get(
                    url,
                    stream=True,
                    timeout=(10, 30),
                    allow_redirects=True,
                )
                if response.status_code in (401, 403, 404):
                    raise RuntimeError(
                        f"HTTP {response.status_code} downloading {_redact_url(url)}"
                    )
                response.raise_for_status()

                total_size = int(response.headers.get(
                    "content-length", 0)) or None

                if show_progress:
                    with open(tmp_path, mode) as f:
                        with tqdm(
                            desc=save_file_path.name,
                            total=total_size,
                            unit="B",
                            unit_scale=True,
                            unit_divisor=1024,
                            leave=False,
                        ) as pbar:
                            for chunk in response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                                    pbar.update(len(chunk))
                else:
                    with open(tmp_path, mode) as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)

                if tmp_path.stat().st_size == 0:
                    raise RuntimeError(
                        f"Downloaded file is empty: {save_file_path.name}")

                tmp_path.replace(save_file_path)
                if not show_progress:
                    tqdm.write(f"✅ Downloaded: '{save_file_path.name}'")
                return

            except requests.exceptions.RequestException as exc:
                last_exc = exc
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                if attempt < max_attempts:
                    time.sleep(min(2 ** (attempt - 1), 15))
                else:
                    raise RuntimeError(
                        f"Network error downloading {_redact_url(url)}: {exc}"
                    ) from exc
            except Exception:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                raise

        if last_exc:
            raise last_exc

    def _save_reading_material(self, reading_material_id: str, save_file_path: Path) -> None:
        """Download and save reading material atomically."""
        if save_file_path.exists() and save_file_path.stat().st_size > 0 and not self.overwrite:
            return

        params = {
            "batch": "1",
            "input": json.dumps({"0": {"json": {"materialId": reading_material_id}}}),
        }

        response = self.session.get(
            f"{self.api_base_url}/course.getReadingMaterial",
            params=params,
            timeout=(10, 30),
        )
        if response.status_code in (401, 403, 404):
            raise RuntimeError(
                f"HTTP {response.status_code} fetching reading material '{reading_material_id}'"
            )
        response.raise_for_status()

        try:
            data = response.json()
            content = data[0]["result"]["data"]["json"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Unexpected API response for reading material '{reading_material_id}'"
            ) from exc

        if not isinstance(content, str):
            raise RuntimeError(
                f"Reading material '{reading_material_id}' returned non-text content")

        save_file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = save_file_path.with_name(f"{save_file_path.name}.part")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        tmp_path.replace(save_file_path)
        tqdm.write(f"✅ Downloaded: '{save_file_path.name}'")

    def _extract_video_and_caption_urls(self, video_id: int) -> Dict[str, Optional[str]]:
        """Extract video and caption URLs from the API with strict validation."""
        params = {
            "batch": "1",
            "input": json.dumps({"0": {"json": {"videoId": video_id}}}),
        }

        response = self.session.get(
            f"{self.api_base_url}/course.getLessonVideo",
            params=params,
            timeout=(10, 30),
        )

        if response.status_code in (401, 403, 404):
            raise RuntimeError(
                f"HTTP {response.status_code} fetching video metadata (videoId={video_id}). "
                "Check authentication cookies or course access."
            )
        response.raise_for_status()

        try:
            payload = response.json()
            data = payload[0]["result"]["data"]["json"]["video"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Unexpected video API response format for videoId={video_id}") from exc

        if not isinstance(data, dict):
            raise RuntimeError(
                f"Empty video metadata returned for videoId={video_id}")

        caption_url: Optional[str] = None
        tracks = data.get("tracks")
        if isinstance(tracks, list) and tracks:
            for track in tracks:
                if isinstance(track, dict) and track.get("src"):
                    caption_url = str(track["src"])
                    break

        video_url = data.get("mp4Url") or data.get("webmUrl")
        if not video_url:
            raise RuntimeError(
                f"No video URL ('mp4Url' or 'webmUrl') returned for videoId={video_id}")

        return {"caption_url": caption_url, "video_url": str(video_url)}

    def download_video_from_m3u8(self, m3u8_url: str) -> Path:
        """Download a video and remux it into the requested container using FFmpeg."""
        if self.video_container in ("mp4", "mkv") and not is_ffmpeg_available():
            raise RuntimeError(
                f"FFmpeg is required to remux HLS/MPEG-TS streams into a valid .{self.video_container} file, "
                "but 'ffmpeg' was not found on PATH. Install FFmpeg (e.g. 'winget install Gyan.FFmpeg') "
                "or pass '--video-container source'."
            )

        if self.video_container in ("mp4", "mkv"):
            expected_path = self.out_dir / \
                f"{self.file_stem}.{self.video_container}"
            if expected_path.exists() and expected_path.stat().st_size > 0 and not self.overwrite:
                if self.video_container != "mp4" or detect_media_container(expected_path) == "mp4":
                    return expected_path

        opts = self._build_opts()
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(m3u8_url, download=True)
                prepared_path = Path(
                    ydl.prepare_filename(info)) if info else None
        except DownloadError as exc:
            raise RuntimeError(f"yt-dlp download/remux failed: {exc}") from exc
        finally:
            if self._pbar is not None:
                self._pbar.close()
                self._pbar = None

        if self.video_container in ("mp4", "mkv"):
            final_path = self.out_dir / \
                f"{self.file_stem}.{self.video_container}"
        else:
            final_path = prepared_path or self.out_dir / \
                f"{self.file_stem}.mp4"

        if not final_path.exists() or final_path.stat().st_size == 0:
            raise RuntimeError(
                f"Expected output video file was not created or is empty: {final_path.name}")

        if self.video_container == "mp4":
            detected = detect_media_container(final_path)
            if detected == "mpegts":
                raise RuntimeError(
                    f"Output '{final_path.name}' still contains raw MPEG-TS instead of an MP4 container. "
                    "Verify your FFmpeg installation or try '--video-container mkv'."
                )

        tqdm.write(f"✅ Downloaded: '{final_path.name}'")
        return final_path

    def download_lesson_content(self, lesson_data: Lesson) -> bool:
        """
        Download a single lesson (reading material or video).
        Returns True on verified success, False on failure (details stored in `self.last_error`).
        """
        self.last_error = None
        try:
            lesson_type = lesson_data.type

            if lesson_type == "reading_material" and lesson_data.readingMaterialId:
                self._save_reading_material(
                    lesson_data.readingMaterialId,
                    self.out_dir / f"{self.file_stem}.md",
                )
            elif lesson_type in ("video", "video_notebook"):
                if not lesson_data.videoId:
                    raise RuntimeError(
                        f"Video lesson '{lesson_data.name}' is missing videoId")
                lesson_video_data = self._extract_video_and_caption_urls(
                    lesson_data.videoId)
                if lesson_video_data.get("caption_url"):
                    self._save_file(
                        # type: ignore[arg-type]
                        lesson_video_data["caption_url"],
                        self.out_dir / f"{self.file_stem}.vtt",
                        is_binary=True,
                        show_progress=False,
                    )
                if lesson_video_data.get("video_url"):
                    self.download_video_from_m3u8(
                        # type: ignore[arg-type]
                        lesson_video_data["video_url"])

            return True
        except Exception as exc:
            self.last_error = str(exc)
            tqdm.write(
                f"❌ Error downloading lesson {lesson_data.index} ({lesson_data.name}): {exc}")
            return False
