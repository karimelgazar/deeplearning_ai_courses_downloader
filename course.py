from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import requests
from tqdm import tqdm

from downloader import YTDLDownloader, detect_media_container, sanitize_filename
from models import CourseData, CoursePartner, DownloadSummary, Lesson
from urls import DeepLearningAIURLs

# Thread-safe lock for progress bar and summary updates
_pbar_lock = Lock()


class CourseStateTracker:
    """Thread-safe atomic state file tracker for reliable resume behavior."""

    def __init__(self, course_dir: Path) -> None:
        self.course_dir = course_dir
        self.state_file = course_dir / ".dl_state.json"
        self._lock = Lock()
        self._state: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.state_file.exists():
            return {}
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def is_completed(self, lesson_key: str, lesson: Lesson, file_stem: str, video_container: str) -> bool:
        entry = self._state.get(lesson_key)
        if not entry or entry.get("status") != "completed":
            return False

        # Non-downloadable lesson types (quiz/program/chatbot) have no local media files
        if lesson.type not in ("video", "video_notebook", "reading_material"):
            return True

        if lesson.type == "reading_material" and lesson.readingMaterialId:
            md_path = self.course_dir / f"{file_stem}.md"
            return md_path.exists() and md_path.stat().st_size > 0

        if lesson.type in ("video", "video_notebook") and lesson.videoId:
            if video_container in ("mp4", "mkv"):
                vid_path = self.course_dir / f"{file_stem}.{video_container}"
                if not vid_path.exists() or vid_path.stat().st_size == 0:
                    return False
                if video_container == "mp4" and detect_media_container(vid_path) == "mpegts":
                    return False
                return True
            else:
                matches = [
                    p
                    for p in self.course_dir.glob(f"{file_stem}.*")
                    if p.suffix.lower() not in (".vtt", ".part", ".md") and p.stat().st_size > 0
                ]
                return len(matches) > 0

        return True

    def mark(self, lesson_key: str, status: str, file_stem: str, error: Optional[str] = None) -> None:
        with self._lock:
            self._state[lesson_key] = {
                "status": status,
                "file_stem": file_stem,
                "error": error,
            }
            self.course_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = self.state_file.with_suffix(".json.tmp")
            tmp_file.write_text(json.dumps(
                self._state, indent=2), encoding="utf-8")
            tmp_file.replace(self.state_file)


def _format_released_at(released_at: Any) -> Optional[str]:
    """Safely parse ISO release timestamp (including Z suffix on Python 3.8-3.10)."""
    if not isinstance(released_at, str) or not released_at.strip():
        return None
    normalized = released_at.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).strftime("%d %B %Y")
    except ValueError:
        return released_at.strip()


def _build_unique_file_stems(sorted_lessons: List[Tuple[str, Lesson]]) -> Dict[str, str]:
    """
    Build collision-free Windows-safe file stems while keeping original lesson names
    untouched in CourseData / Markdown metadata.
    """
    stems: Dict[str, str] = {}
    seen_lower: Dict[str, int] = {}

    for lesson_key, lesson_data in sorted_lessons:
        base_stem = f"{lesson_data.index:02d}_{sanitize_filename(lesson_data.name)}"
        key_lower = base_stem.lower()
        count = seen_lower.get(key_lower, 0)
        seen_lower[key_lower] = count + 1
        stems[lesson_key] = base_stem if count == 0 else f"{base_stem}_{count + 1}"

    return stems


class Course:
    """
    Course class for downloading DeepLearning.AI courses.
    """

    def __init__(self, course_data: Dict[str, Any], session: requests.Session):
        self._data = CourseData(**course_data)
        self.session = session

    @classmethod
    def build_from_url(cls, course_url_or_slug: str, session: requests.Session) -> "Course":
        course_slug = cls._extract_course_slug(course_url_or_slug)
        raw_data = cls._fetch_raw_data(course_slug, session)
        return cls.build_from_raw_data(raw_data, session)

    @classmethod
    def build_from_raw_data(cls, raw_data: Dict[str, Any], session: requests.Session) -> "Course":
        """
        Build a Course instance from raw API response data.
        Safely handles missing optional metadata such as `wpData: null` and missing `releasedAt`.
        """
        if not isinstance(raw_data, dict):
            raise ValueError("Invalid course payload: expected a JSON object")

        for required_key in ("name", "slug", "courseId"):
            if not raw_data.get(required_key):
                raise ValueError(
                    f"Course payload is missing required field: '{required_key}'")

        wp_data = raw_data.get("wpData")
        if not isinstance(wp_data, dict):
            wp_data = {}

        final_data = {
            "lessons": raw_data.get("lessons") or {},
            "course_info": {
                "name": raw_data["name"],
                "slug": raw_data["slug"],
                "type": raw_data.get("type") or "course",
                "courseId": raw_data["courseId"],
                "releasedAt": _format_released_at(raw_data.get("releasedAt")),
                "coursePartner": wp_data.get("coursePartner") or [],
                "courseTopic": wp_data.get("courseTopic") or [],
                "courseLevel": wp_data.get("courseLevel"),
                "courseThumbnail": wp_data.get("videoThumbnail"),
            },
        }

        return cls(final_data, session)

    @staticmethod
    def _extract_course_slug(course_id: str) -> str:
        """Extract course slug from a course or lesson URL, or return slug as-is."""
        if not course_id or not str(course_id).strip():
            raise ValueError("Course id is required")

        course_slug = str(course_id).strip().rstrip("/")
        if course_slug.startswith(DeepLearningAIURLs.COURSES_BASE):
            remainder = course_slug[len(
                DeepLearningAIURLs.COURSES_BASE):].lstrip("/")
            course_slug = remainder.split("/")[0].split("?")[0].split("#")[0]

        if not course_slug:
            raise ValueError(
                f"Could not extract course slug from: {course_id}")

        return course_slug

    @staticmethod
    def _fetch_raw_data(course_slug: str, session: requests.Session) -> Dict[str, Any]:
        """Fetch course data from the DeepLearning.AI tRPC API."""
        params = {
            "batch": "1",
            "input": json.dumps({"0": {"json": {"courseSlug": course_slug}}}),
        }

        response = session.get(
            DeepLearningAIURLs.GET_COURSE_BY_SLUG,
            params=params,
            timeout=(10, 30),
        )

        if response.status_code in (401, 403):
            raise RuntimeError(
                f"Authentication failed (HTTP {response.status_code}) while fetching course '{course_slug}'. "
                "Your cookies may be expired or invalid."
            )
        if response.status_code == 404:
            raise RuntimeError(f"Course '{course_slug}' not found (HTTP 404).")

        response.raise_for_status()

        try:
            data = response.json()[0]["result"]["data"]["json"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Unexpected API response structure for course '{course_slug}'") from exc

        if not isinstance(data, dict):
            raise RuntimeError(
                f"Course '{course_slug}' returned empty data. Check the slug or your authentication cookies."
            )

        return data

    def download(
        self,
        save_dir: Path,
        concurrent_downloads: int = 1,
        video_container: str = "mp4",
        retry_count: int = 3,
        resume: bool = True,
        overwrite: bool = False,
        verbose: bool = False,
    ) -> DownloadSummary:
        """
        Download the entire course and return an accurate DownloadSummary.
        Progress bar tracks processed lessons (successful + skipped + failed).
        """
        tqdm.write("Getting course content list...")
        course_info = self._data.course_info
        lessons = self._data.lessons

        summary = DownloadSummary(
            course_slug=course_info.slug,
            total_lessons=len(lessons),
        )

        if not lessons:
            tqdm.write(f"No lessons found for course {course_info.slug}")
            return summary

        tqdm.write("Saving course info...")
        save_dir.mkdir(parents=True, exist_ok=True)
        self._save_course_info_as_markdown(save_dir / "00_course_info.md")

        sorted_lessons = sorted(
            lessons.items(), key=lambda x: x[1].index if x[1] else 0)
        file_stems = _build_unique_file_stems(sorted_lessons)
        tracker = CourseStateTracker(save_dir)

        tqdm.write(
            f"Downloading {len(sorted_lessons)} lessons "
            f"(concurrent: {concurrent_downloads}, container: {video_container}, resume: {resume})..."
        )

        # Progress bar represents processed lessons (successful + skipped + failed)
        pbar = tqdm(
            total=len(sorted_lessons),
            desc=f"Processing Course: {course_info.slug}",
            unit="lesson",
            leave=True,
            position=0,
        )

        def run_single(lesson_key: str, lesson_data: Lesson, slot: int) -> None:
            stem = file_stems[lesson_key]

            if resume and not overwrite and tracker.is_completed(lesson_key, lesson_data, stem, video_container):
                with _pbar_lock:
                    summary.skipped += 1
                    pbar.update(1)
                return

            downloader = YTDLDownloader(
                lesson_data=lesson_data,
                out_dir=save_dir,
                session=self.session,
                position=slot,
                video_container=video_container,
                retry_count=retry_count,
                overwrite=overwrite,
                verbose=verbose,
                file_stem_override=stem,
            )
            success = downloader.download_lesson_content(lesson_data)
            if success:
                tracker.mark(lesson_key, "completed", file_stem=stem)
                with _pbar_lock:
                    summary.successful += 1
                    pbar.update(1)
            else:
                reason = downloader.last_error or "Unknown error"
                tracker.mark(lesson_key, "failed",
                             file_stem=stem, error=reason)
                with _pbar_lock:
                    summary.failed += 1
                    summary.failed_lessons.append(
                        f"{lesson_data.index:02d}_{lesson_data.name} ({reason})")
                    pbar.update(1)

        try:
            if concurrent_downloads <= 1:
                for lesson_key, lesson_data in sorted_lessons:
                    run_single(lesson_key, lesson_data, slot=1)
            else:
                with ThreadPoolExecutor(max_workers=concurrent_downloads) as executor:
                    future_to_meta = {}
                    for idx, (lesson_key, lesson_data) in enumerate(sorted_lessons):
                        slot = 1 + (idx % concurrent_downloads)
                        future = executor.submit(
                            run_single, lesson_key, lesson_data, slot)
                        future_to_meta[future] = (lesson_key, lesson_data)

                    for future in as_completed(future_to_meta):
                        lesson_key, lesson_data = future_to_meta[future]
                        try:
                            future.result()
                        except Exception as exc:
                            stem = file_stems[lesson_key]
                            tracker.mark(lesson_key, "failed",
                                         file_stem=stem, error=str(exc))
                            with _pbar_lock:
                                summary.failed += 1
                                summary.failed_lessons.append(
                                    f"{lesson_data.index:02d}_{lesson_data.name} ({exc})"
                                )
                                pbar.update(1)
        finally:
            pbar.close()

        self._print_summary(summary, save_dir)
        return summary

    @staticmethod
    def _print_summary(summary: DownloadSummary, save_dir: Path) -> None:
        tqdm.write("\n" + "=" * 60)
        if summary.is_complete:
            tqdm.write(f"Course downloaded successfully to: '{save_dir}'")
        else:
            tqdm.write(
                f"Download completed with errors for '{summary.course_slug}'")
            tqdm.write(f"Output directory: '{save_dir}'")

        tqdm.write(f"Total lessons: {summary.total_lessons}")
        tqdm.write(f"Successful:    {summary.successful}")
        tqdm.write(f"Skipped:       {summary.skipped}")
        tqdm.write(f"Failed:        {summary.failed}")

        if summary.failed_lessons:
            tqdm.write("\nFailed lessons:")
            for item in summary.failed_lessons:
                tqdm.write(f"  - {item}")
            tqdm.write(
                "\nTip: Re-run the same command to resume and retry failed lessons.")
        tqdm.write("=" * 60)

    def _save_course_info_as_markdown(self, save_file_path: Path) -> None:
        """Save course information as a markdown file."""
        course_info = self._data.course_info
        lessons = self._data.lessons

        save_file_path.parent.mkdir(parents=True, exist_ok=True)
        md_lines = [f"# {course_info.name}\n"]

        if course_info.courseThumbnail:
            md_lines.append(
                f"![Course Thumbnail]({course_info.courseThumbnail})\n")

        md_lines.append("---\n")
        md_lines.append("## Course Information\n\n")

        if course_info.slug:
            course_url = f"{DeepLearningAIURLs.COURSES_BASE}/{course_info.slug}"
            md_lines.append(
                f"- **Course Link**: [{course_info.name}]({course_url})\n")

        md_lines.append(f"- **Course ID**: `{course_info.courseId}`\n")
        md_lines.append(
            f"- **Type**: {course_info.type.replace('_', ' ').title()}\n")
        md_lines.append(
            f"- **Released**: {course_info.releasedAt or 'Unknown'}\n")

        if course_info.coursePartner:
            partners = []
            for partner in course_info.coursePartner:
                if isinstance(partner, CoursePartner):
                    if partner.logo:
                        partners.append(
                            f"{partner.title}\n![{partner.title}]({partner.logo})")
                    else:
                        partners.append(partner.title)
            if partners:
                md_lines.append(f"- **Partner**: {' | '.join(partners)}\n")

        md_lines.append(
            f"- **Level**: {course_info.courseLevel or 'Unknown'}\n")

        if course_info.courseTopic:
            topics_str = ", ".join(course_info.courseTopic)
            md_lines.append(f"- **Topics**: {topics_str}\n")

        md_lines.append("\n---\n")
        md_lines.append("## Lessons\n\n")

        sorted_lessons = sorted(
            lessons.items(), key=lambda x: x[1].index if x[1] else 0)

        for _, lesson_data in sorted_lessons:
            lesson_index = lesson_data.index
            lesson_name = lesson_data.name
            lesson_type = lesson_data.type
            lesson_slug = lesson_data.slug
            lesson_time = lesson_data.time or 0

            time_str = ""
            if lesson_time:
                minutes = lesson_time // 60
                seconds = lesson_time % 60
                if minutes > 0:
                    time_str = f"{minutes}m {seconds}s" if seconds > 0 else f"{minutes}m"
                else:
                    time_str = f"{seconds}s"

            lesson_url = "{base}/{slug}/lesson/{lesson_slug}/{lesson_name}".format(
                base=DeepLearningAIURLs.COURSES_BASE,
                slug=course_info.slug,
                lesson_slug=lesson_slug,
                lesson_name=lesson_name.replace(" ", "-"),
            )

            type_icon = {
                "video": "🎥",
                "video_notebook": "🎥",
                "reading_material": "📄",
                "quiz": "❓",
                "program": "💻",
                "chatbot": "💬",
            }.get(lesson_type, "📌")

            md_lines.append(
                f"### {lesson_index}. {type_icon} {lesson_name}\n\n")
            md_lines.append(
                f"- **Type**: {lesson_type.replace('_', ' ').title()}\n")
            if time_str:
                md_lines.append(f"- **Duration**: {time_str}\n")
            md_lines.append(f"- **Link**: [{lesson_name}]({lesson_url})\n")

            if lesson_type == "video" and lesson_data.videoId:
                md_lines.append(f"- **Video ID**: `{lesson_data.videoId}`\n")
            if lesson_type == "reading_material" and lesson_data.readingMaterialId:
                md_lines.append(
                    f"- **Reading Material ID**: `{lesson_data.readingMaterialId}`\n")
            if lesson_type == "quiz" and lesson_data.quizId:
                md_lines.append(f"- **Quiz ID**: `{lesson_data.quizId}`\n")

            md_lines.append("\n")

        with open(save_file_path, "w", encoding="utf-8") as f:
            f.write("".join(md_lines))

    @property
    def name(self) -> str:
        return self._data.course_info.name

    @property
    def slug(self) -> str:
        return self._data.course_info.slug

    @property
    def lesson_count(self) -> int:
        return len(self._data.lessons)

    def __repr__(self) -> str:
        return f"Course(name='{self.name}', slug='{self.slug}', lessons={self.lesson_count})"
