"""Sphinx configuration for the battfeed documentation site."""

from __future__ import annotations

from datetime import date
from importlib.metadata import version as _pkg_version
from pathlib import Path

_DOCS = Path(__file__).resolve().parent
_ROOT = _DOCS.parent
_GENERATED = _DOCS / "_generated"
_BLOB = "https://github.com/DigiBatt/battfeed/blob/main/"

project = "battfeed"
author = "Simon Clark and contributors"
copyright = f"{date.today().year}, the battfeed contributors. Apache-2.0"
release = _pkg_version("battfeed")
version = release

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx_design",
    "sphinx_copybutton",
]

exclude_patterns = ["_build", "_generated", "Thumbs.db", ".DS_Store"]

myst_enable_extensions = ["colon_fence", "deflist", "fieldlist"]
myst_heading_anchors = 4

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

autodoc_member_order = "bysource"
autodoc_typehints = "description"

html_theme = "pydata_sphinx_theme"
html_title = "battfeed"
html_theme_options = {
    "github_url": "https://github.com/DigiBatt/battfeed",
    "icon_links": [
        {
            "name": "PyPI",
            "url": "https://pypi.org/project/battfeed/",
            "icon": "fa-brands fa-python",
        },
    ],
    "navbar_align": "left",
    "logo": {"text": "battfeed"},
    "footer_start": ["copyright"],
    "footer_end": [],
}
html_context = {
    "github_user": "DigiBatt",
    "github_repo": "battfeed",
    "github_version": "main",
    "doc_path": "docs",
}

copybutton_prompt_text = r">>> |\.\.\. |\$ "
copybutton_prompt_is_regexp = True


def _rewrite_repo_links(text: str) -> str:
    """Point repo-relative links in root documents at the site or GitHub.

    The generated copies are included by pages under ``docs/project`` and
    ``docs/reference``; source-tree targets go to GitHub.
    """
    text = text.replace("](CHANGELOG.md)", "](../reference/changelog.md)")
    text = text.replace("](ROADMAP.md)", "](roadmap.md)")
    for target in ("IMPLEMENTATION.md", "README.md", "src/", "examples/", "tests/", "LICENSE", "NOTICE"):
        text = text.replace(f"]({target}", f"]({_BLOB}{target}")
    return text


def _generate_includes() -> None:
    """Copy the canonical root documents into the docs tree at build time.

    CHANGELOG.md, ROADMAP.md, and CONTRIBUTING.md stay the single source of
    truth at the repository root; the site renders these generated,
    link-rewritten copies (gitignored) via ``include`` directives.
    """
    _GENERATED.mkdir(exist_ok=True)
    for src, dest in (
        ("CHANGELOG.md", "changelog.md"),
        ("ROADMAP.md", "roadmap.md"),
        ("CONTRIBUTING.md", "contributing.md"),
    ):
        text = _rewrite_repo_links((_ROOT / src).read_text(encoding="utf-8"))
        (_GENERATED / dest).write_text(text, encoding="utf-8")


_generate_includes()
