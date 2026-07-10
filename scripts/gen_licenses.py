#!/usr/bin/env python
"""Generate third-party license attribution files from installed package metadata.

Writes:
  - ``LICENSE``                  — the Apache License 2.0 full text (this
    project's own license; also satisfies the "include the license text"
    obligation for all bundled Apache-2.0 dependencies, since it is the same
    text).
  - ``THIRD_PARTY_LICENSES.txt`` — the full per-package list (name, version,
    license summary, URL) followed by the full texts of the standard licenses
    (Apache-2.0, MIT, BSD-3-Clause) bundled by this distribution.

No network access required: it reads everything from
``importlib.metadata`` (installed dist-info). Run inside the Docker image,
which has the full dependency tree installed::

    python scripts/gen_licenses.py

Re-run whenever the dependency set changes (e.g. after an AutoGluon bump).
"""
from __future__ import annotations

import importlib.metadata as md
from pathlib import Path

OUT = Path("THIRD_PARTY_LICENSES.txt")
LICENSE = Path("LICENSE")
# Filenames dist-info commonly uses for license/notice text.
LICENSE_FILENAMES = ("LICENSE", "LICENSE.txt", "LICENSE.md", "LICENSE.MD", "COPYING", "NOTICE")


def _license_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    """Return [(filename, text), ...] for any license/notice files in the dist-info."""
    found: list[tuple[str, str]] = []
    for f in dist.files or []:
        if f.name in LICENSE_FILENAMES:
            try:
                p = f.locate()
                if p and Path(p).is_file():
                    txt = Path(p).read_text(encoding="utf-8", errors="replace")
                    if txt.strip():
                        found.append((f.name, txt))
            except Exception:
                continue
    return found


def _pick(texts: list[tuple[str, str]], marker: str) -> str | None:
    for _name, txt in texts:
        if marker in txt:
            return txt
    return None


def main() -> None:
    rows: list[tuple[str, str, str, str]] = []
    apache_text: str | None = None
    mit_text: str | None = None
    bsd_text: str | None = None

    for dist in sorted(md.distributions(), key=lambda d: (d.metadata.get("Name") or "").lower()):
        name = dist.metadata.get("Name") or ""
        ver = dist.metadata.get("Version") or ""
        lic = (dist.metadata.get("License") or "(unspecified)").splitlines()[0][:80]
        url = dist.metadata.get("Home-page") or ""
        if not url:
            project_url = dist.metadata.get("Project-URL") or ""
            url = project_url.split(",", 1)[-1].strip() if project_url else ""
        rows.append((name, ver, lic, url))

        texts = _license_texts(dist)
        if apache_text is None and "Apache" in lic and "2.0" in lic:
            apache_text = _pick(texts, "Apache License") or apache_text
        if mit_text is None and "MIT" in lic:
            mit_text = _pick(texts, "Permission is hereby granted") or _pick(texts, "MIT License") or mit_text
        if bsd_text is None and "BSD" in lic:
            bsd_text = _pick(texts, "Redistribution and use") or bsd_text

    # Project LICENSE = Apache-2.0 full text.
    if not apache_text:
        raise SystemExit("Could not locate Apache-2.0 license text in any dist-info; refusing to write LICENSE.")
    LICENSE.write_text(apache_text if apache_text.endswith("\n") else apache_text + "\n", encoding="utf-8")

    lines: list[str] = [
        "THIRD-PARTY SOFTWARE NOTICES AND LICENSES",
        "=" * 70,
        "",
        "This distribution bundles the third-party packages listed below. Each",
        "package is licensed under the terms shown in its row. The full text of",
        "the standard open-source licenses used here (Apache-2.0, MIT, BSD-3-Clause)",
        "is appended once at the end of this file.",
        "",
        f"{'Package':36} {'Version':14} {'License':28} URL",
        "-" * 110,
    ]
    for name, ver, lic, url in rows:
        if not name:
            continue
        lines.append(f"{name[:36]:36} {ver[:14]:14} {lic[:28]:28} {url}")
    lines.append("")

    def _section(title: str, text: str) -> None:
        lines.extend(["", "=" * 70, title, "=" * 70, text.rstrip()])

    if apache_text:
        _section("Apache License 2.0 (full text)", apache_text)
    if mit_text:
        _section("MIT License (full text)", mit_text)
    if bsd_text:
        _section("BSD 3-Clause License (full text)", bsd_text)

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes)")
    print(f"Wrote {LICENSE} ({LICENSE.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
