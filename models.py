from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator


class CoursePartner(BaseModel):
    """Model for course partner information."""

    title: str
    logo: Optional[str] = None


class CourseInfo(BaseModel):
    """Model for course metadata."""

    name: str
    slug: str
    type: str = "course"
    courseId: int
    releasedAt: Optional[str] = None
    coursePartner: List[Union[CoursePartner, Dict[str, Any]]] = Field(
        default_factory=list)
    courseTopic: List[str] = Field(default_factory=list)
    courseLevel: Optional[str] = None
    courseThumbnail: Optional[str] = None

    @field_validator("coursePartner", mode="before")
    @classmethod
    def validate_course_partner(cls, v: Any) -> List[CoursePartner]:
        """Convert dict or string partners to CoursePartner objects safely."""
        if not v:
            return []
        if isinstance(v, list):
            result: List[CoursePartner] = []
            for partner in v:
                if isinstance(partner, CoursePartner):
                    result.append(partner)
                elif isinstance(partner, dict):
                    title = partner.get("title") or partner.get("name")
                    if title:
                        result.append(CoursePartner(
                            title=str(title), logo=partner.get("logo")))
                elif isinstance(partner, str) and partner.strip():
                    result.append(CoursePartner(title=partner.strip()))
            return result
        return []

    @field_validator("courseTopic", mode="before")
    @classmethod
    def validate_course_topic(cls, v: Any) -> List[str]:
        """Normalize topic list safely."""
        if not v:
            return []
        if isinstance(v, list):
            topics: List[str] = []
            for item in v:
                if isinstance(item, str) and item.strip():
                    topics.append(item.strip())
                elif isinstance(item, dict):
                    topic_name = item.get("name") or item.get("title")
                    if topic_name:
                        topics.append(str(topic_name))
            return topics
        return []


class Lesson(BaseModel):
    """Model for a lesson."""

    index: int
    slug: str
    name: str
    type: str
    videoId: Optional[int] = None
    time: Optional[int] = None
    programId: Optional[Union[str, int]] = None
    chatbotId: Optional[Union[str, int]] = None
    iframeUrl: Optional[str] = None
    quizId: Optional[str] = None
    progress: Optional[int] = None
    readingMaterialId: Optional[str] = None
    accessControl: Optional[str] = None
    requiredUserTier: Optional[str] = None
    features: Optional[Dict[str, Any]] = None


class CourseData(BaseModel):
    """Model for complete course data."""

    lessons: Dict[str, Lesson] = Field(default_factory=dict)
    course_info: CourseInfo

    @field_validator("lessons", mode="before")
    @classmethod
    def validate_lessons(cls, v: Any) -> Dict[str, Lesson]:
        """Convert dict lessons to Lesson objects."""
        if not v:
            return {}
        if isinstance(v, dict):
            result: Dict[str, Lesson] = {}
            for key, lesson in v.items():
                if isinstance(lesson, dict):
                    result[key] = Lesson(**lesson)
                elif isinstance(lesson, Lesson):
                    result[key] = lesson
            return result
        return {}


class DownloadSummary(BaseModel):
    """Accurate summary of a course download run."""

    course_slug: str
    total_lessons: int
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    failed_lessons: List[str] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.failed == 0 and (self.successful + self.skipped) == self.total_lessons
