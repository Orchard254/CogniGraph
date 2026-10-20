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
            self.copy_file(str(source), str(build_root / filename))

    def get_outputs(self, include_bytecode=1):
        outputs = super().get_outputs(include_bytecode=include_bytecode)
        return outputs + [str(Path(self.build_lib) / name) for name in _SCHEMA_FILES]


setup(cmdclass={"build_py": BuildPyWithSchema})
