"""
tests/test_workspace.py — Tests for workspace creation logic.
"""
import json
import re
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import workspace
from workspace import _make_folder_name, create_workspace


# ── _make_folder_name ─────────────────────────────────────────────────────────

class TestMakeFolderName:
    def test_starts_with_today(self):
        today = date.today().isoformat()
        name = _make_folder_name("KTH", "PhD in Physics")
        assert name.startswith(today)

    def test_lowercased(self):
        name = _make_folder_name("KTH", "PhD Position")
        assert name == name.lower()

    def test_special_chars_replaced_with_dash(self):
        name = _make_folder_name("KTH Royal", "PhD: Plasma/Fusion")
        assert " " not in name
        assert ":" not in name
        assert "/" not in name

    def test_no_double_dashes(self):
        name = _make_folder_name("A  B", "C  D")
        # replace -- pattern after normalization
        assert "--" not in name

    def test_company_truncated_to_20(self):
        long_company = "A" * 30
        name = _make_folder_name(long_company, "Title")
        # company part (between first _ and second _) should be <= 20 chars
        parts = name.split("_", 2)  # [date, company_part, title_part]
        assert len(parts[1]) <= 20

    def test_title_truncated_to_30(self):
        long_title = "B" * 40
        name = _make_folder_name("Co", long_title)
        parts = name.split("_", 2)
        assert len(parts[2]) <= 30


# ── create_workspace ──────────────────────────────────────────────────────────

def _sample_job(**kwargs) -> dict:
    defaults = {
        "id": "phd_001", "title": "PhD in Plasma Physics",
        "company": "KTH", "direction": "phd",
        "score": "8", "url": "https://kth.se/job/1",
        "reason": "Strong plasma background",
        "research_fit": "Plasma fusion", "deadline": "2026-05-01",
        "application_method": "web", "contact_email": "",
        "location": "Stockholm",
    }
    return {**defaults, **kwargs}


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """
    Patch JOBS_DIR and INDEX to use tmp_path.
    Also chdir to tmp_path so that folder.relative_to(Path(".")) works
    (workspace.py uses Path(".") as the base for TASK.md link generation).
    """
    monkeypatch.chdir(tmp_path)
    jobs_dir = tmp_path / "jobs"
    index    = tmp_path / "jobs_index.csv"
    with patch.object(workspace, "JOBS_DIR", jobs_dir), \
         patch.object(workspace, "INDEX", index):
        yield tmp_path, jobs_dir


class TestCreateWorkspace:
    def test_creates_folder_structure(self, ws_env):
        _, jobs_dir = ws_env
        job = _sample_job()
        create_workspace(job)
        # Find the created folder
        folders = list(jobs_dir.iterdir())
        assert len(folders) == 1
        folder = folders[0]
        assert (folder / "cl").is_dir()
        assert (folder / "cv").is_dir()

    def test_creates_required_files(self, ws_env):
        _, jobs_dir = ws_env
        create_workspace(_sample_job())
        folder = list(jobs_dir.iterdir())[0]
        for fname in ["TASK.md", "job_info.md", "forms.md", "notes.md", "status.json"]:
            assert (folder / fname).exists(), f"{fname} not found"

    def test_status_json_contains_job_id(self, ws_env):
        _, jobs_dir = ws_env
        job = _sample_job(id="phd_999")
        create_workspace(job)
        folder = list(jobs_dir.iterdir())[0]
        status = json.loads((folder / "status.json").read_text())
        assert status["job_id"] == "phd_999"

    def test_status_json_has_timeline(self, ws_env):
        _, jobs_dir = ws_env
        create_workspace(_sample_job())
        folder = list(jobs_dir.iterdir())[0]
        status = json.loads((folder / "status.json").read_text())
        assert len(status["timeline"]) == 1
        assert status["timeline"][0]["event"] == "workspace_created"

    def test_task_md_contains_job_title(self, ws_env):
        _, jobs_dir = ws_env
        job = _sample_job(title="Plasma Researcher", company="LTU")
        create_workspace(job)
        folder = list(jobs_dir.iterdir())[0]
        task_content = (folder / "TASK.md").read_text(encoding="utf-8")
        assert "Plasma Researcher" in task_content
        assert "LTU" in task_content

    def test_task_md_references_template_dir(self, ws_env):
        _, jobs_dir = ws_env
        create_workspace(_sample_job(direction="job"))
        folder = list(jobs_dir.iterdir())[0]
        task_content = (folder / "TASK.md").read_text(encoding="utf-8")
        assert "data/base_templates/" in task_content

    def test_task_md_shows_legitimacy_verdict(self, ws_env):
        _, jobs_dir = ws_env
        create_workspace(_sample_job(legit_verdict="pass", legit_score="8.5"))
        folder = list(jobs_dir.iterdir())[0]
        task_content = (folder / "TASK.md").read_text(encoding="utf-8")
        assert "PASS" in task_content
        assert "8.5" in task_content

    def test_job_info_md_contains_score(self, ws_env):
        _, jobs_dir = ws_env
        create_workspace(_sample_job(score="9"))
        folder = list(jobs_dir.iterdir())[0]
        content = (folder / "job_info.md").read_text(encoding="utf-8")
        assert "9/10" in content

    def test_idempotent_second_call_returns_existing(self, ws_env):
        _, jobs_dir = ws_env
        job = _sample_job()
        folder1 = create_workspace(job)
        folder2 = create_workspace(job)
        assert folder1 == folder2
        assert len(list(jobs_dir.iterdir())) == 1

    def test_forms_md_has_personal_info_section(self, ws_env):
        _, jobs_dir = ws_env
        create_workspace(_sample_job())
        folder = list(jobs_dir.iterdir())[0]
        content = (folder / "forms.md").read_text(encoding="utf-8")
        assert "Personal info" in content
