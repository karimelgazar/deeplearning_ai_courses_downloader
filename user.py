from __future__ import annotations

import json
from typing import Any, Dict, List

import requests

from urls import DeepLearningAIURLs


class User:
    def __init__(self, session: requests.Session):
        self.session = session
        self.enrolled_curriculums_v2_endpoint = DeepLearningAIURLs.ENROLLED_CURRICULUMS_V2

    def _list_enrolled_courses(self, filter: str = "studying") -> List[Dict[str, Any]]:
        if filter not in ("studying", "finished"):
            raise ValueError("filter must be either 'studying' or 'finished'")

        params = {
            "batch": "1",
            "input": json.dumps({"0": {"json": {"filter": filter}}}),
        }

        response = self.session.get(
            self.enrolled_curriculums_v2_endpoint,
            params=params,
            timeout=(10, 30),
        )

        if response.status_code in (401, 403):
            raise RuntimeError(
                f"Authentication rejected (HTTP {response.status_code}) while listing {filter} courses. "
                "Your cookies may be expired or missing."
            )

        if not response.ok:
            raise RuntimeError(
                f"Failed to list {filter} courses: HTTP {response.status_code}")

        try:
            data = response.json()[0]["result"]["data"]["json"]["courses"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Unexpected API response while listing {filter} courses") from exc

        return data if isinstance(data, list) else []

    def list_studying_courses(self) -> List[Dict[str, Any]]:
        return self._list_enrolled_courses(filter="studying")

    def list_finished_courses(self) -> List[Dict[str, Any]]:
        return self._list_enrolled_courses(filter="finished")

    def list_all_courses(self) -> List[Dict[str, Any]]:
        return self._list_enrolled_courses(filter="studying") + self._list_enrolled_courses(filter="finished")
