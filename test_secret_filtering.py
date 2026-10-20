"""Credential-filter regression coverage for memory intake and replacement."""
import pytest

from memory_store import MemoryStore
from pm_common import looks_like_secret


@pytest.mark.parametrize(
    "candidate",
    [
        "OPENAI_API_KEY=sk-proj-" + "A" * 40,
        "SERVICE_SECRET_TOKEN=" + "A" * 40,
        "github_pat_" + "A" * 40,
        "xoxb-" + "A" * 40,
        "AIza" + "A" * 35,
        "npm_" + "A" * 40,
        "pypi-" + "A" * 40,
        "AWS_SECRET_ACCESS_KEY=" + "A" * 40,
        "private_key=" + "A" * 50,
        "sk_live_" + "A" * 32,
        "rk_test_" + "A" * 32,
        "glpat-" + "A" * 40,
    ],
)
def test_detects_common_credentials_and_prefixed_environment_keys(candidate):
    assert looks_like_secret(candidate)


def test_does_not_flag_environment_variable_name_without_value():
    assert not looks_like_secret("Read OPENAI_API_KEY from the configured environment.")


def test_write_rejects_secret_in_tags(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    try:
        with pytest.raises(ValueError, match="secret"):
            store.write(
                category="component",
                content="Safe fact about deployment configuration.",
                tags="github_pat_" + "A" * 40,
                force=True,
                embed=False,
            )
    finally:
        store.close()


def test_supersede_rejects_secret_in_replacement_tags(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    try:
        fact_id = store.write(
            category="component",
            content="Safe original deployment fact.",
            tags="deployment",
            force=True,
            embed=False,
        )
        with pytest.raises(ValueError, match="secret"):
            store.supersede(
                fact_id,
                "Safe revised deployment fact.",
                tags="OPENAI_API_KEY=sk-proj-" + "A" * 40,
            )
    finally:
        store.close()

def test_deprecate_rejects_secret_in_reason(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    try:
        fact_id = store.write(
            category="component",
            content="Safe original deployment fact.",
            force=True,
            embed=False,
        )
        secret = "github_pat_" + "A" * 40
        with pytest.raises(ValueError, match="secret"):
            store.deprecate(fact_id, reason=f"obsolete; backup token={secret}")
        with pytest.raises(ValueError, match="secret"):
            store.deprecate_many([fact_id], reason=f"obsolete; token={secret}")
    finally:
        store.close()


def test_flag_fact_rejects_secret_in_persisted_metadata(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    try:
        fact_id = store.write(
            category="component",
            content="Safe original deployment fact.",
            force=True,
            embed=False,
        )
        secret = "xoxb-" + "A" * 40
        with pytest.raises(ValueError, match="secret"):
            store.flag_fact(fact_id, note=f"Credential was {secret}")
        with pytest.raises(ValueError, match="secret"):
            store.flag_fact(fact_id, flag_type=secret)
    finally:
        store.close()

def test_write_rejects_secret_in_persisted_file_metadata(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    try:
        secret = "OPENAI_API_KEY=sk-proj-" + "A" * 40
        with pytest.raises(ValueError, match="secret"):
            store.write(
                category="component",
                content="Safe deployment fact.",
                session_id=secret,
                force=True,
                embed=False,
            )
        with pytest.raises(ValueError, match="secret"):
            store.write(
                category="component",
                content="Safe deployment fact.",
                file_path="src/" + "github_pat_" + "A" * 40,
                force=True,
                embed=False,
            )
    finally:
        store.close()


def test_supersede_rejects_secret_in_replacement_metadata(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "memory.db"))
    try:
        fact_id = store.write(
            category="component",
            content="Safe original deployment fact.",
            force=True,
            embed=False,
        )
        with pytest.raises(ValueError, match="secret"):
            store.supersede(
                fact_id,
                "Safe revised deployment fact.",
                session_id="xoxb-" + "A" * 40,
            )
    finally:
        store.close()

