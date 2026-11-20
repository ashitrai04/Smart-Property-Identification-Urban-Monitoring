"""Build building_delineator.zip, the file you install in QGIS.

The plugin repository rejects a zip whose structure is wrong, and QGIS itself
fails quietly on some of it, so the checks here are the same ones the review
applies: one top-level folder, named as the plugin's Python package, with
metadata.txt directly inside it, and no compiled or editor leftovers.

Run:  python package.py
"""
import configparser
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = "building_delineator"
SRC = os.path.join(HERE, PKG)
OUT = os.path.join(HERE, f"{PKG}.zip")

# Never ship these: __pycache__ breaks on a different Python, and the rest is
# noise that makes the download bigger and the review slower.
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode", ".pytest_cache"}
SKIP_EXT = {".pyc", ".pyo", ".orig", ".rej", ".swp"}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}

REQUIRED_META = ["name", "qgisMinimumVersion", "description", "version", "author",
                 "email", "about", "tracker", "repository", "license"]


def check_compiles():
    """Every .py must compile before it can be packaged.

    QGIS imports a plugin at runtime, so a syntax error does not surface until
    someone clicks the button - and then it surfaces as a dialog full of
    traceback. A zip shipped with one is worse than no zip. This is cheap and
    catches the whole class.
    """
    bad = []
    for root, dirs, names in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in sorted(names):
            if not n.endswith(".py"):
                continue
            full = os.path.join(root, n)
            rel = os.path.relpath(full, SRC)
            try:
                # compile() rather than py_compile: nothing is written, so
                # there is no cfile to find a home for, and on Windows
                # os.devnull is not a usable one.
                with open(full, encoding="utf-8") as fh:
                    compile(fh.read(), full, "exec")
            except SyntaxError as e:
                bad.append(f"  {rel}:{e.lineno}: {e.msg}")
            except UnicodeDecodeError as e:
                bad.append(f"  {rel}: not valid UTF-8 ({e})")
    if bad:
        sys.exit("will not package - these do not compile:\n" + "\n".join(bad))


def check_metadata():
    path = os.path.join(SRC, "metadata.txt")
    if not os.path.exists(path):
        sys.exit("metadata.txt is missing — QGIS will not load the plugin")
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    if not cp.has_section("general"):
        sys.exit("metadata.txt needs a [general] section")
    missing = [k for k in REQUIRED_META if not cp.get("general", k, fallback="").strip()]
    if missing:
        sys.exit("metadata.txt is missing: " + ", ".join(missing))
    lic = cp.get("general", "license", fallback="")
    if "GPL" not in lic.upper():
        sys.exit(f"license is {lic!r}; a plugin importing qgis.core must be GPL-compatible")
    if not os.path.exists(os.path.join(SRC, "LICENSE")):
        sys.exit("ship the LICENSE file alongside the licence field")
    return cp.get("general", "version"), cp.get("general", "name")


def files():
    for root, dirs, names in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in sorted(names):
            if n in SKIP_NAMES or os.path.splitext(n)[1] in SKIP_EXT:
                continue
            full = os.path.join(root, n)
            # Paths inside the zip must start with the package folder, or QGIS
            # unpacks a plugin it cannot import.
            yield full, os.path.join(PKG, os.path.relpath(full, SRC)).replace(os.sep, "/")


# The branch the plugin updates from. raw.githubusercontent serves whatever is
# committed here, so a push IS a release - there is no separate upload step to
# forget, and no version that exists only on someone's laptop.
RAW_BASE = ("https://raw.githubusercontent.com/ashitrai04/"
            "Smart-Property-Identification-Urban-Monitoring/main/qgis-plugin")
XML = os.path.join(HERE, "plugins.xml")


def write_repo_xml(version):
    """Write the one-entry plugin repository QGIS reads to offer updates.

    Generated from metadata.txt, never hand-written: QGIS compares the version
    in this XML against the installed one, so the two disagreeing is the
    failure that matters - a release that silently never offers itself, with
    nothing on screen to say why.

    Carries no email on purpose. metadata.txt has one because the plugin
    format asks for it; a served XML does not need it, and a contact address
    in a public file is one more place for it to be scraped from.
    """
    from xml.sax.saxutils import escape

    cp = configparser.ConfigParser()
    cp.read(os.path.join(SRC, "metadata.txt"), encoding="utf-8")

    def g(key, default=""):
        # metadata.txt allows a paragraph; XML elements want one line.
        return " ".join(cp.get("general", key, fallback=default).split())

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<plugins>
  <pyqgis_plugin name="{escape(g('name'))}" version="{escape(version)}">
    <description>{escape(g('description'))}</description>
    <about>{escape(g('about'))}</about>
    <version>{escape(version)}</version>
    <qgis_minimum_version>{escape(g('qgisMinimumVersion', '3.22'))}</qgis_minimum_version>
    <homepage>{escape(g('homepage'))}</homepage>
    <file_name>{PKG}.zip</file_name>
    <icon>{escape(g('icon', 'icon.svg'))}</icon>
    <author_name>{escape(g('author'))}</author_name>
    <download_url>{RAW_BASE}/{PKG}.zip</download_url>
    <tracker>{escape(g('tracker'))}</tracker>
    <repository>{escape(g('repository'))}</repository>
    <tags>{escape(g('tags'))}</tags>
    <experimental>{escape(g('experimental', 'False'))}</experimental>
    <deprecated>{escape(g('deprecated', 'False'))}</deprecated>
  </pyqgis_plugin>
</plugins>
"""
    with open(XML, "w", encoding="utf-8") as fh:
        fh.write(xml)


def main():
    check_compiles()
    version, name = check_metadata()
    if os.path.exists(OUT):
        os.remove(OUT)
    n = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for full, arc in files():
            z.write(full, arc)
            n += 1

    with zipfile.ZipFile(OUT) as z:
        names = z.namelist()
        tops = {p.split("/")[0] for p in names}
        if tops != {PKG}:
            sys.exit(f"zip must hold exactly one top-level folder, found {sorted(tops)}")
        for needed in (f"{PKG}/metadata.txt", f"{PKG}/__init__.py", f"{PKG}/LICENSE"):
            if needed not in names:
                sys.exit(f"{needed} missing from the zip")
        bad = [p for p in names if "__pycache__" in p or p.endswith(".pyc")]
        if bad:
            sys.exit(f"compiled files leaked in: {bad[:3]}")

    write_repo_xml(version)

    size = os.path.getsize(OUT) / 1024
    print(f"{name} {version}")
    print(f"  {OUT}")
    print(f"  {n} files, {size:.0f} KB")
    print(f"  {XML}")
    print("")
    print("First time only - add the repository in QGIS:")
    print("  Plugins -> Manage and Install Plugins -> Settings -> Add...")
    print(f"  {RAW_BASE}/plugins.xml")
    print("")
    print("After that, Upgrade appears in the plugin manager on every release.")


if __name__ == "__main__":
    main()
