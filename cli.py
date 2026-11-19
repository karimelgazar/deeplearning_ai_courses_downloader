"""
DeepLearning.AI Course Downloader

Command-line interface for downloading DeepLearning.AI courses.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional

import requests
from tqdm import tqdm

from auth import ENV_COOKIES_FILE, SUPPORTED_BROWSERS, create_session
from course import Course
from downloader import VALID_VIDEO_CONTAINERS, sanitize_filename
from user import User


def prepare_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dl-ai",
        description="Download courses from DeepLearning.AI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download a single course using an exported Netscape cookie file (recommended on Windows)
  dl-ai machine-learning --cookies-file "C:\\path\\to\\cookies.txt"

  # Download a single course using full URL and browser cookies
  dl-ai "https://learn.deeplearning.ai/courses/my-course-slug" --browser chrome

  # Download enrolled courses (studying, finished, or all)
  dl-ai --enrolled all -o ./downloads -c 2

  # Choose target video container (mp4/mkv require FFmpeg; source keeps original stream container)
  dl-ai machine-learning --video-container mp4
        """,
    )

    parser.add_argument(
        "course",
        type=str,
        nargs="?",
        help=(
            "Course URL or slug (e.g., 'my-course-slug' or "
            "'https://learn.deeplearning.ai/courses/my-course-slug'). "
            "Required if --enrolled is not used."
        ),
    )

    parser.add_argument(
        "-e",
        "--enrolled",
        type=str,
        choices=["studying", "finished", "all"],
        help=(
            "Download enrolled courses. Choose 'studying', 'finished', or 'all'. "
            "If specified, course argument is not needed."
        ),
    )

    parser.add_argument(
        "-o",
        "--output-dir",
        type=str,
        default="./courses",
        help="Directory where courses will be saved (default: ./courses)",
    )

    parser.add_argument(
        "-c",
        "--concurrent",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Number of lessons to download concurrently (default: 1). "
            "Higher values can speed up downloads but may hit rate limits."
        ),
    )

    parser.add_argument(
        "-b",
        "--browser",
        type=str,
        choices=SUPPORTED_BROWSERS,
        default="chrome",
        help=(
            f"Browser to extract cookies from when --cookies-file is not used (default: chrome). "
            f"Supported browsers: {', '.join(SUPPORTED_BROWSERS)}"
        ),
    )

    parser.add_argument(
        "--cookies-file",
        type=str,
        default=None,
        help=(
            f"Path to a Netscape/Mozilla format cookies.txt file. "
            f"Takes precedence over {ENV_COOKIES_FILE} and --browser."
        ),
    )

    parser.add_argument(
        "--video-container",
        type=str,
        choices=list(VALID_VIDEO_CONTAINERS),
        default="mp4",
        help=(
            "Target video container: 'mp4' (default, losslessly remuxes HLS MPEG-TS via FFmpeg), "
            "'mkv' (remux via FFmpeg), or 'source' (keep original downloaded container)."
        ),
    )

    parser.add_argument(
        "--retry-count",
        type=int,
        default=3,
        help="Number of retries for transient network/server errors (default: 3).",
    )

    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Disable skipping lessons already recorded as completed in .dl_state.json.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing downloaded files.",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show verbose yt-dlp logs and warnings.",
    )

    args = parser.parse_args(argv)

    if not args.course and not args.enrolled:
        parser.error(
            "Either 'course' argument or '--enrolled' option must be provided")

    if args.course and args.enrolled:
        parser.error(
            "Cannot specify both 'course' argument and '--enrolled' option")

    if args.concurrent < 1:
        parser.error("--concurrent must be at least 1")

    if args.retry_count < 0:
        parser.error("--retry-count must be 0 or greater")

    return args


def download_enrolled_courses(
    args: argparse.Namespace,
    output_dir: Path,
    session: requests.Session,
) -> bool:
    """Download enrolled courses. Returns True if all courses completed without errors."""
    user = User(session)
    courses_data = []

    if args.enrolled == "studying":
        tqdm.write("Fetching studying courses...")
        courses_data = user.list_studying_courses()
        status_name = "studying"
    elif args.enrolled == "finished":
        tqdm.write("Fetching finished courses...")
        courses_data = user.list_finished_courses()
        status_name = "finished"
    elif args.enrolled == "all":
        tqdm.write("Fetching all enrolled courses...")
        courses_data = user.list_all_courses()
        status_name = "enrolled"

    if not courses_data:
        tqdm.write("API access verified. No enrolled courses found.")
        return True

    tqdm.write(
        f"API access verified. Found {len(courses_data)} {status_name} course(s)\n")
    all_ok = True

    for idx, course_data in enumerate(courses_data, 1):
        course_slug = course_data.get("slug")
        course_name = course_data.get("name", "Unknown")

        if not course_slug:
            tqdm.write(f"Skipping course {idx}: No slug found")
            continue

        tqdm.write(f"[{idx}/{len(courses_data)}] Processing: {course_name}")
        tqdm.write(f"  Slug: {course_slug}")

        try:
            course = Course.build_from_raw_data(course_data, session)
            tqdm.write(f"  Lessons: {course.lesson_count}")

            save_dir = output_dir / sanitize_filename(course.slug)
            tqdm.write(f"  Saving to: {save_dir}\n")
            summary = course.download(
                save_dir,
                concurrent_downloads=args.concurrent,
                video_container=args.video_container,
                retry_count=args.retry_count,
                resume=not args.no_resume,
                overwrite=args.overwrite,
                verbose=args.verbose,
            )
            if not summary.is_complete:
                all_ok = False
        except Exception as exc:
            all_ok = False
            tqdm.write(f"  ✗ Error downloading {course_name}: {exc}\n")

        if idx < len(courses_data):
            tqdm.write("  Waiting 60 seconds before next course...\n")
            time.sleep(60)

    if all_ok:
        tqdm.write(
            f"✅ All {len(courses_data)} course(s) downloaded successfully!")
    else:
        tqdm.write("⚠️ Finished processing enrolled courses with some errors.")
    return all_ok


def download_single_course(
    args: argparse.Namespace,
    output_dir: Path,
    session: requests.Session,
) -> bool:
    """Download a single course. Returns True if completed without lesson failures."""
    course_url_or_slug = args.course.strip()

    tqdm.write(
        f"Verifying API access and loading course: {course_url_or_slug}")
    course = Course.build_from_url(course_url_or_slug, session=session)
    tqdm.write(f"Found course: {course.name}")
    tqdm.write(f"  Slug: {course.slug}")
    tqdm.write(f"  Lessons: {course.lesson_count}")

    save_dir = output_dir / sanitize_filename(course.slug)
    tqdm.write(f"\nSaving to: {save_dir}")
    summary = course.download(
        save_dir,
        concurrent_downloads=args.concurrent,
        video_container=args.video_container,
        retry_count=args.retry_count,
        resume=not args.no_resume,
        overwrite=args.overwrite,
        verbose=args.verbose,
    )
    return summary.is_complete


def main(argv: Optional[List[str]] = None) -> None:
    """Main entry point for the CLI script."""
    args = prepare_args(argv)

    try:
        session = create_session(
            browser=args.browser,
            cookies_file=args.cookies_file,
            retry_count=args.retry_count,
        )
        auth_source = getattr(session, "auth_source", args.browser)
        tqdm.write(
            f"Loaded cookies from {auth_source} (verifying with DeepLearning.AI API...)\n")
    except Exception as exc:
        tqdm.write(f"❌ Failed to load cookies: {exc}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir).expanduser().resolve()

    try:
        if args.enrolled:
            all_ok = download_enrolled_courses(args, output_dir, session)
        else:
            all_ok = download_single_course(args, output_dir, session)

        if not all_ok:
            sys.exit(1)

    except KeyboardInterrupt:
        tqdm.write(
            "\n\nDownload interrupted by user. Completed lessons were saved for resume.")
        sys.exit(130)
    except Exception as exc:
        tqdm.write(f"\n✗ Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
