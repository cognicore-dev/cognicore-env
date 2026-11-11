"""cognicore lesson: record mistakes once, find the relevant ones before acting."""
import pytest

from cognicore import lessons


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNICORE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data" / lessons.DB_NAME


def _fixes(task, project="/work/app"):
    return [r["fix"] for r in lessons.check(task, project=project)]


FIVE_MISTAKES = [  # (mistake, fix, why, when, a later task worded differently)
    ("Ran npm install", "Use pnpm, not npm", "npm breaks the pnpm lockfile",
     "installing or adding packages, dependencies", "install a package: add axios"),
    ("Edited files under vendor/", "Never edit vendor/; patch the upstream package", "vendor/ is regenerated on build",
     "changing third-party or vendored code", "change the vendored date library"),
    ("Wrote dates as MM/DD/YYYY", "Use ISO dates, YYYY-MM-DD", "the billing API rejects other formats",
     "writing or formatting dates", "format dates in the invoice export"),
    ("Ran pytest without the env file", "Run tests with `make test`, which loads .env.test", "tests need DATABASE_URL",
     "running tests", "run the tests for the auth module"),
    ("Used single quotes in Python", "Use double quotes in Python code", "the team's formatter enforces them",
     "writing Python code style", "write a Python helper for slug generation"),
]


@pytest.mark.parametrize("mistake,fix,why,when,later_task", FIVE_MISTAKES)
def test_each_lesson_is_recorded_once_and_found_in_a_fresh_session(db, mistake, fix, why, when, later_task):
    res = lessons.add(mistake, fix, why, when, project="/work/app")
    assert res == {"id": res["id"], "merged": False, "seen": 1}
    # A fresh session is a fresh process: only the database carries over.
    found = lessons.check(later_task, project="/work/app")
    assert [r["fix"] for r in found][:1] == [fix]
    assert found[0]["why"] == why


def test_all_five_together_each_task_gets_its_own_lesson(db):
    for mistake, fix, why, when, _ in FIVE_MISTAKES:
        lessons.add(mistake, fix, why, when, project="/work/app")
    for _, fix, _, _, later_task in FIVE_MISTAKES:
        found = _fixes(later_task)
        assert fix in found and len(found) <= lessons.CHECK_LIMIT, (later_task, found)


def test_the_same_lesson_reworded_is_merged_and_counted(db):
    first = lessons.add("Ran npm install in this repo", "Use pnpm, not npm", "npm breaks the pnpm lockfile",
                        project="/work/app")
    again = lessons.add("Used npm install here", "Use pnpm instead of npm", "npm rewrites the lockfile",
                        project="/work/app")
    assert again == {"id": first["id"], "merged": True, "seen": 2}
    assert len(lessons.list_lessons(project="/work/app")) == 1
    assert lessons.list_lessons(project="/work/app")[0]["why"] == "npm breaks the pnpm lockfile"  # the more detailed reason


def test_different_lessons_are_not_merged(db):
    lessons.add("Ran npm install", "Use pnpm, not npm", project="/work/app")
    lessons.add("Pushed to main", "Open a pull request instead of pushing to main", project="/work/app")
    assert len(lessons.list_lessons(project="/work/app")) == 2


def test_check_prints_nothing_when_nothing_matches(db):
    lessons.add("Ran npm install", "Use pnpm, not npm", "npm breaks the lockfile", "installing packages",
                project="/work/app")
    assert lessons.check("rename the header component", project="/work/app") == []
    assert lessons.check("the a to", project="/work/app") == []  # stopwords only


def test_check_stays_within_three_lessons_and_the_token_budget(db):
    for i in range(10):
        lessons.add(f"Mistake {i} with the deploy script step{i}", f"Deploy fix number {i} for step{i}",
                    "because " + "the deploy pipeline is strict " * 6, "deploying", project="/work/app")
    found = lessons.check("deploy the service", project="/work/app")
    assert 1 <= len(found) <= lessons.CHECK_LIMIT
    assert sum(len(lessons.format_line(r)) // 4 + 1 for r in found) <= lessons.CHECK_TOKENS


def test_project_lessons_stay_in_their_project_and_global_ones_apply_everywhere(db):
    lessons.add("Ran npm install", "Use pnpm, not npm", when="installing packages", project="/work/app")
    lessons.add("Committed a .env file", "Never commit .env files", when="committing changes", scope="global")
    assert _fixes("install a package", project="/work/other") == []
    assert _fixes("committing changes to git", project="/work/other") == ["Never commit .env files"]
    assert _fixes("install a package", project="/work/app") == ["Use pnpm, not npm"]


def test_secrets_are_redacted_before_storing(db):
    lessons.add("Pasted the key sk-ant-api03-" + "x" * 40 + " into config.py",
                "Read the key from the environment", "keys must not be in code", project="/work/app")
    stored = lessons.list_lessons(project="/work/app")[0]
    assert "sk-ant-api03" not in stored["mistake"] and "[REDACTED" in stored["mistake"]


def test_a_lesson_needs_a_mistake_and_a_fix(db):
    with pytest.raises(ValueError):
        lessons.add("", "Use pnpm", project="/work/app")


def test_forget_removes_a_lesson_from_list_and_check(db):
    res = lessons.add("Ran npm install", "Use pnpm, not npm", when="installing packages", project="/work/app")
    assert lessons.forget(res["id"]) is True
    assert lessons.forget(res["id"]) is False
    assert _fixes("install a package") == []


def test_cli_add_check_list_forget(db, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert lessons.main(["add", "--mistake", "Ran npm install", "--fix", "Use pnpm, not npm",
                         "--why", "npm breaks the lockfile", "--when", "installing packages"]) == 0
    assert "saved as lesson #1" in capsys.readouterr().out
    assert lessons.main(["check", "install", "a", "package:", "add", "axios"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Lessons from earlier mistakes") and "Use pnpm, not npm (why: npm breaks the lockfile)" in out
    assert lessons.main(["check", "rename", "the", "header"]) == 0
    assert capsys.readouterr().out == ""  # nothing to apply, nothing printed
    assert lessons.main(["list"]) == 0 and "#1" in capsys.readouterr().out
    assert lessons.main(["forget", "1"]) == 0 and lessons.main(["forget", "1"]) == 1


def test_skill_manifest_matches_the_openclaw_format():
    import pathlib
    import re

    text = (pathlib.Path(__file__).parent.parent / "skills" / "never-twice" / "SKILL.md").read_text(encoding="utf-8")
    front = text.split("---")[1]
    assert re.search(r"^name: never-twice$", front, re.M)  # must match the folder name
    assert re.search(r"^description: .{20,}", front, re.M)
    assert re.search(r"^version: \d+\.\d+\.\d+$", front, re.M)
    assert re.search(r"requires:\s*\n\s*bins:\s*\n\s*- cognicore", front)
    assert "cognicore lesson check" in text and "cognicore lesson add" in text
