"""Setuptools build hooks for non-package runtime schema resources."""
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


_SCHEMA_FILES = (
    "phase0_schema.sql",
    "phase1_episodic.sql",
    "phase1_episodic_embeddings.sql",
    "phase2_branch_admin.sql",
    "episodic_fts.sql",
    "legacy_provenance.sql",
)


class BuildPyWithSchema(build_py):
    """Place SQL resources beside the top-level Python modules in wheels."""

    def run(self):
        super().run()
        source_root = Path(__file__).resolve().parent
        build_root = Path(self.build_lib)
        for filename in _SCHEMA_FILES:
            source = source_root / filename
            if not source.is_file():
                raise FileNotFoundError(f"Required CogniGraph schema resource is missing: {source}")
            target = build_root / filename
            self.copy_file(str(source), str(target))
            self.outputs.append(str(target))


setup(cmdclass={"build_py": BuildPyWithSchema})
