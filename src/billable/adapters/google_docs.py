"""Google Docs activity adapter.

Captures TWO kinds of activity per (doc, day):

    1. Edits — via the Drive Activity API (`activity.query`). Catches
       create/edit/comment/move/etc.
    2. Views — via the Drive API's `viewedByMeTime` field. Catches the
       common "I opened a doc someone shared with me to review it" case
       that Drive Activity does not surface.

A doc that was both viewed and edited on the same day produces ONE Event
whose `actions` list contains both labels.

Required OAuth scopes:
    https://www.googleapis.com/auth/drive.activity.readonly
    https://www.googleapis.com/auth/drive.metadata.readonly

(We deliberately avoid requesting the broader documents.readonly scope at
v1 — the doc title + activity metadata is enough for the LLM to write a
useful narrative. We can add doc-content fetching later if narratives feel
too thin.)

Setup is documented in README.md "Google OAuth (one-time)".

Each emitted Event represents "the user touched DOC X on the target date":

    timestamp        - the latest activity timestamp on that date
    duration         - None (neither API gives us focus time)
    project_hint     - the doc's title (the mapper can keyword-match on it)
    artifact_ref     - "gdoc:<doc_id>"
    content_excerpt  - human-readable summary like "viewed, edited 'Title'"
    raw              - {doc_id, folder_ids, actions, edit_count, ...}
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from billable.adapters.base import CaptureAdapter
from billable.core.events import Event
from billable.core.sanitize import sanitize

log = logging.getLogger(__name__)

SCOPES: list[str] = [
    "https://www.googleapis.com/auth/drive.activity.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]


class GoogleDocsAdapter(CaptureAdapter):
    name = "gdocs"

    def __init__(
        self,
        client_secrets_path: Path,
        token_cache_path: Path,
    ) -> None:
        self.client_secrets_path = client_secrets_path
        self.token_cache_path = token_cache_path

    def capture(self, target_date: date) -> list[Event]:
        if not self.client_secrets_path.exists():
            log.warning(
                "Google OAuth client secrets not found at %s; skipping gdocs capture.",
                self.client_secrets_path,
            )
            return []

        creds = self._ensure_credentials()
        from googleapiclient.discovery import build

        activity_svc = build("driveactivity", "v2", credentials=creds, cache_discovery=False)
        drive_svc = build("drive", "v3", credentials=creds, cache_discovery=False)

        # Local-day boundary in UTC for the API filter.
        local_tz = datetime.now().astimezone().tzinfo
        day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=local_tz)
        day_end = day_start + timedelta(days=1)
        start_ms = int(day_start.astimezone(timezone.utc).timestamp() * 1000)
        end_ms = int(day_end.astimezone(timezone.utc).timestamp() * 1000)

        # Aggregate per-document so we emit one Event per (doc, day).
        # Maps doc_id -> {"latest_ts": datetime, "edit_count": int, "actions": [str]}
        per_doc: dict[str, dict[str, Any]] = {}

        # --- Pass 1: edits / comments / moves via Drive Activity ---------
        page_token: str | None = None
        while True:
            request_body = {
                "pageSize": 100,
                "filter": f"time >= {start_ms} AND time < {end_ms}",
            }
            if page_token:
                request_body["pageToken"] = page_token
            response = activity_svc.activity().query(body=request_body).execute()

            for activity in response.get("activities", []):
                self._absorb_activity(activity, per_doc, local_tz)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        # --- Pass 2: views via Drive's viewedByMeTime --------------------
        # Drive Activity API has no "view" action; this is how we catch
        # "I opened a doc someone shared with me to review it." We only
        # have one timestamp per (doc, user) so we cannot count views, only
        # mark the doc as viewed-today.
        self._absorb_views(drive_svc, per_doc, day_start, day_end, local_tz)

        # --- Build Events --------------------------------------------------
        events: list[Event] = []
        for doc_id, agg in per_doc.items():
            try:
                title, folder_ids = self._fetch_doc_metadata(drive_svc, doc_id)
            except Exception as e:
                log.warning("Could not fetch metadata for doc %s: %s", doc_id, e)
                title, folder_ids = ("(untitled)", [])

            unique_actions = sorted(set(agg["actions"]))
            actions_summary = ", ".join(unique_actions) or "touched"
            excerpt = sanitize(
                f"{actions_summary} '{title}' ({agg['edit_count']} activity event"
                f"{'s' if agg['edit_count'] != 1 else ''})."
            )

            events.append(
                Event(
                    timestamp=agg["latest_ts"],
                    duration=None,
                    source="gdocs",
                    project_hint=title,
                    artifact_ref=f"gdoc:{doc_id}",
                    content_excerpt=excerpt,
                    raw={
                        "doc_id": doc_id,
                        "folder_ids": folder_ids,
                        "edit_count": agg["edit_count"],
                        "actions": unique_actions,
                    },
                )
            )

        events.sort(key=lambda e: e.timestamp)
        return events

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _absorb_activity(
        activity: dict[str, Any],
        per_doc: dict[str, dict[str, Any]],
        local_tz: object,
    ) -> None:
        """Update per_doc aggregations from one Drive Activity event."""
        # Pull the timestamp (timestamp or timeRange.endTime).
        ts_str: str | None = activity.get("timestamp")
        if not ts_str and isinstance(activity.get("timeRange"), dict):
            ts_str = activity["timeRange"].get("endTime")
        if not ts_str:
            return
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(local_tz)
        except ValueError:
            return

        # Action labels (e.g. "edit", "create", "comment").
        actions = []
        for action in activity.get("actions", []):
            detail = action.get("detail") or {}
            for k in detail.keys():
                actions.append(k)
        if not actions:
            actions = ["edit"]

        # Find every document target on this activity. Activities can affect
        # multiple targets; we only care about Drive items that look like Docs.
        for target in activity.get("targets", []):
            drive_item = target.get("driveItem")
            if not drive_item:
                continue
            mime = drive_item.get("mimeType")
            if mime and mime != "application/vnd.google-apps.document":
                continue
            name = drive_item.get("name") or ""  # like "items/<doc_id>"
            doc_id = name.split("/", 1)[1] if "/" in name else name
            if not doc_id:
                continue

            agg = per_doc.setdefault(
                doc_id,
                {"latest_ts": ts, "edit_count": 0, "actions": []},
            )
            agg["edit_count"] += 1
            agg["actions"].extend(actions)
            if ts > agg["latest_ts"]:
                agg["latest_ts"] = ts

    @staticmethod
    def _absorb_views(
        drive_svc: Any,
        per_doc: dict[str, dict[str, Any]],
        day_start: datetime,
        day_end: datetime,
        local_tz: object,
    ) -> None:
        """Find Google Docs the user viewed on the target date.

        Drive's `viewedByMeTime` is updated whenever the authenticated user
        opens a file. It only stores the LAST view time, which is fine — we
        want one event per (doc, day) anyway.

        We page through `files.list` with a Drive query filter, restricted to
        Google Docs the current user can see.
        """
        start_iso = day_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_iso = day_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        q = (
            f"viewedByMeTime >= '{start_iso}' "
            f"AND viewedByMeTime < '{end_iso}' "
            f"AND mimeType = 'application/vnd.google-apps.document'"
        )

        page_token: str | None = None
        while True:
            request = drive_svc.files().list(
                q=q,
                fields="nextPageToken, files(id,name,parents,viewedByMeTime)",
                pageSize=100,
                pageToken=page_token,
                spaces="drive",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            try:
                response = request.execute()
            except Exception as e:
                log.warning("Drive view query failed: %s", e)
                return

            for f in response.get("files", []):
                doc_id = f.get("id")
                if not doc_id:
                    continue
                viewed_str = f.get("viewedByMeTime")
                if not viewed_str:
                    continue
                try:
                    ts = datetime.fromisoformat(viewed_str.replace("Z", "+00:00")).astimezone(
                        local_tz
                    )
                except ValueError:
                    continue

                agg = per_doc.setdefault(
                    doc_id,
                    {"latest_ts": ts, "edit_count": 0, "actions": []},
                )
                if "view" not in agg["actions"]:
                    agg["actions"].append("view")
                if ts > agg["latest_ts"]:
                    agg["latest_ts"] = ts

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    @staticmethod
    def _fetch_doc_metadata(drive_svc: Any, doc_id: str) -> tuple[str, list[str]]:
        """Return (title, ancestor_folder_ids) for a doc."""
        meta = (
            drive_svc.files()
            .get(fileId=doc_id, fields="id,name,parents", supportsAllDrives=True)
            .execute()
        )
        return meta.get("name", "(untitled)"), list(meta.get("parents") or [])

    def _ensure_credentials(self) -> Any:
        """Load cached OAuth credentials or run the consent flow.

        Caches the refreshed token at `self.token_cache_path` after every run.
        """
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds: Credentials | None = None
        if self.token_cache_path.exists():
            try:
                creds = Credentials.from_authorized_user_info(
                    json.loads(self.token_cache_path.read_text(encoding="utf-8")),
                    SCOPES,
                )
            except (ValueError, json.JSONDecodeError) as e:
                log.warning("Cached Google token unusable, re-running consent: %s", e)
                creds = None

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                log.warning("Google token refresh failed, re-running consent: %s", e)
                creds = None

        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets_path), SCOPES
            )
            creds = flow.run_local_server(port=0)

        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_cache_path.write_text(creds.to_json(), encoding="utf-8")
        return creds
