import os
import pathlib
import re
import subprocess

from setuptools import find_packages, setup

here = pathlib.Path(__file__).parent.resolve()
BASE_VERSION = "1.0.13"


def _normalize_commit(value: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", value)[:12].lower()


def _detect_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=here, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ""
    return _normalize_commit(out.strip())


def _build_version() -> str:
    # Prefer an explicit commit from CI; otherwise derive it from the local git checkout.
    commit = _normalize_commit(str(os.environ.get("STACKY_BUILD_COMMIT", "")))
    if not commit:
        commit = _detect_commit()
    if commit:
        return f"{BASE_VERSION}+g{commit}"
    return BASE_VERSION


# Get the long description from the README file
long_description = (here / "README.md").read_text(encoding="utf-8")

setup(
    name="rockset-stacky",
    version=_build_version(),
    description="""
    stacky is a tool to manage stacks of PRs. This allows developers to easily 
    manage many smaller, more targeted PRs that depend on each other.
    """,
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/rockset/stacky",
    author="Rockset",
    author_email="tudor@rockset.com",
    keywords="github, stack, pr, pull request",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    python_requires=">=3.8, <4",
    install_requires=["asciitree", "ansicolors", "simple-term-menu"],
    entry_points={
        "console_scripts": [
            "stacky=stacky:main",
        ],
    },
    project_urls={
        "Bug Reports": "https://github.com/rockset/stacky/issues",
        "Source": "https://github.com/rockset/stacky",
    },
)
