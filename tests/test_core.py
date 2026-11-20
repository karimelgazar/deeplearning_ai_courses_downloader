from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from auth import AuthenticationError, ENV_COOKIES_FILE, create_session, load_cookies_from_file
from course import Course, _build_unique_file_stems
from downloader import YTDLDownloader, detect_media_container, sanitize_filename
from models import Lesson


# --- 1. Authentication & Cookie File Tests ---

def test_load_cookies_file_validation(tmp_path: Path) -> None:
    with pytest.raises(AuthenticationError, match="not found"):
        load_cookies_from_file(tmp_path / "does_not_exist.txt")

    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("", encoding="utf-8")
    with pytest.raises(AuthenticationError, match="empty"):
        load_cookies_from_file(empty_file)

    invalid_file = tmp_path / "bad.txt"
    invalid_file.write_text("not a netscape cookie file\n", encoding="utf-8")
    with pytest.raises(AuthenticationError, match="Invalid Netscape"):
        load_cookies_from_file(invalid_file)


def test_create_session_from_cookies_file_and_env_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_cookie = tmp_path / "cli-cookies.txt"
    env_cookie = tmp_path / "env-cookies.txt"
    sample = (
        "# Netscape HTTP Cookie File\n"
        ".deeplearning.ai\tTRUE\t/\tTRUE\t2147483647\tsession_token\tsuper_secret_val\n"
    )
    cli_cookie.write_text(sample, encoding="utf-8")
    env_cookie.write_text(sample, encoding="utf-8")

    monkeypatch.setenv(ENV_COOKIES_FILE, str(env_cookie))

    # CLI argument must override environment variable
    session_cli = create_session(cookies_file=str(cli_cookie))
    assert getattr(
        session_cli, "auth_source") == "--cookies-file (cli-cookies.txt)"
    assert "super_secret_val" not in getattr(session_cli, "auth_source")

    # Environment variable used when CLI argument is omitted
    session_env = create_session()
    assert getattr(
        session_env, "auth_source") == f"{ENV_COOKIES_FILE} (env-cookies.txt)"


# --- 2. Course Parsing & Null wpData Tests ---

def test_build_course_with_null_wpdata_and_missing_released_at() -> None:
    raw_payload = {
        "courseId": 1066,
        "name": "Supervised Machine Learning: Regression and Classification",
        "slug": "machine-learning",
        "type": "course",
        "releasedAt": None,
        "wpData": None,
        "lessons": {
            "yjrbu": {
                "index": 3,
                "slug": "motivations",
                "name": "What is machine learning?",
                "type": "video",
                "videoId": 12345,
                "time": 360,
            }
        },
    }
    course = Course.build_from_raw_data(raw_payload, requests.Session())
    assert course.slug == "machine-learning"
    assert course.lesson_count == 1
    assert course._data.course_info.coursePartner == []
    assert course._data.course_info.courseTopic == []
    assert course._data.course_info.releasedAt is None
    assert course._data.lessons["yjrbu"].name == "What is machine learning?"


def test_extract_course_slug_from_course_and_lesson_urls() -> None:
    assert (
        Course._extract_course_slug(
            "https://learn.deeplearning.ai/courses/machine-learning")
        == "machine-learning"
    )
    assert (
        Course._extract_course_slug(
            "https://learn.deeplearning.ai/courses/machine-learning/lesson/yjrbu/motivations"
        )
        == "machine-learning"
    )
    assert Course._extract_course_slug(
        "machine-learning") == "machine-learning"


# --- 3. Filename Sanitization & Collision Tests ---

def test_sanitize_filename_windows_rules() -> None:
    assert sanitize_filename(
        "What is machine learning?") == "What is machine learning_"
    assert sanitize_filename('A/B\\C:D*E?"F"<G>|H') == "A_B_C_D_E_F_G_H"
    assert sanitize_filename(
        "Trailing spaces and dots...   ") == "Trailing spaces and dots"
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("nul.vtt") == "_nul.vtt"
    assert sanitize_filename("COM9") == "_COM9"
    assert sanitize_filename("   ") == "untitled"
    assert sanitize_filename("یادگیری ماشین؟") == "یادگیری ماشین؟"


def test_unique_file_stems_prevent_collisions() -> None:
    lessons = [
        ("a", Lesson(index=1, slug="l1", name="Intro?", type="video")),
        ("b", Lesson(index=1, slug="l2", name="Intro:", type="video")),
    ]
    stems = _build_unique_file_stems(lessons)
    assert stems["a"] == "01_Intro_"
    assert stems["b"] == "01_Intro__2"


# --- 4. Container Detection & FFmpeg Remux Options Tests ---

def test_detect_media_container(tmp_path: Path) -> None:
    mp4_file = tmp_path / "valid.mp4"
    mp4_file.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
    assert detect_media_container(mp4_file) == "mp4"

    ts_file = tmp_path / "fake.mp4"
    ts_packet = b"\x47" + b"\x00" * 187
    ts_file.write_bytes(ts_packet * 3)
    assert detect_media_container(ts_file) == "mpegts"

    mkv_file = tmp_path / "video.mkv"
    mkv_file.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 32)
    assert detect_media_container(mkv_file) == "mkv"


def test_ytdl_opts_include_ffmpeg_remuxer_and_missing_ffmpeg_fails(tmp_path: Path) -> None:
    lesson = Lesson(index=1, slug="intro", name="What is ML?",
                    type="video", videoId=99)
    dl_mp4 = YTDLDownloader(
        lesson, tmp_path, requests.Session(), video_container="mp4")
    opts = dl_mp4._build_opts()
    assert opts["merge_output_format"] == "mp4"
    assert opts["skip_unavailable_fragments"] is False
    assert {"key": "FFmpegVideoRemuxer",
            "preferedformat": "mp4"} in opts["postprocessors"]

    dl_src = YTDLDownloader(
        lesson, tmp_path, requests.Session(), video_container="source")
    opts_src = dl_src._build_opts()
    assert opts_src["skip_unavailable_fragments"] is False
    assert "postprocessors" not in opts_src

    with patch("downloader.is_ffmpeg_available", return_value=False):
        with pytest.raises(RuntimeError, match="FFmpeg is required"):
            dl_mp4.download_video_from_m3u8(
                "https://example.com/playlist.m3u8")


# --- 5. Accurate Progress Accounting & Resume State Tests ---

def test_course_download_summary_reports_partial_failure_and_resume(tmp_path: Path) -> None:
    raw_payload = {
        "courseId": 1066,
        "name": "Test Course",
        "slug": "test-course",
        "wpData": None,
        "lessons": {
            "l1": {"index": 1, "slug": "l1", "name": "Good Lesson", "type": "reading_material", "readingMaterialId": "r1"},
            "l2": {"index": 2, "slug": "l2", "name": "Bad Lesson?", "type": "reading_material", "readingMaterialId": "r2"},
        },
    }
    course = Course.build_from_raw_data(raw_payload, requests.Session())

    def fake_download(self: YTDLDownloader, lesson_data: Lesson) -> bool:
        if lesson_data.slug == "l1":
            (self.out_dir /
             f"{self.file_stem}.md").write_text("# Content", encoding="utf-8")
            return True
        self.last_error = "Simulated timeout"
        return False

    with patch.object(YTDLDownloader, "download_lesson_content", fake_download):
        summary1 = course.download(
            tmp_path, concurrent_downloads=1, resume=True)

    assert summary1.total_lessons == 2
    assert summary1.successful == 1
    assert summary1.failed == 1
    assert summary1.skipped == 0
    assert not summary1.is_complete

    # Second run with resume=True should skip l1 and retry l2
    def fake_download_retry(self: YTDLDownloader, lesson_data: Lesson) -> bool:
        (self.out_dir /
         f"{self.file_stem}.md").write_text("# Retried", encoding="utf-8")
        return True

    with patch.object(YTDLDownloader, "download_lesson_content", fake_download_retry):
        summary2 = course.download(
            tmp_path, concurrent_downloads=1, resume=True)

    assert summary2.skipped == 1
    assert summary2.successful == 1
    assert summary2.failed == 0
    assert summary2.is_complete
