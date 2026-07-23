"""Tests for the robust path-resolution + document-reading primitives
(resolve_path, read_document) added to forge.tools.

These exist to replace the model's hand-rolled `Path(name).exists()` +
`pdftotext` cell code, which failed on bare filenames living in ~/Downloads
and swallowed failures. The primitives resolve loosely-named paths and fail
loudly + honestly.
"""
from __future__ import annotations

import pytest

from forge.tools import (
    FileResolutionError,
    ProtectedPathError,
    read_document,
    resolve_path,
)

# ---- resolve_path -----------------------------------------------------------


def test_resolve_path_direct_hit(tmp_path):
    p = tmp_path / "file.txt"
    p.write_text("x")
    assert resolve_path(str(p)) == p


def test_resolve_path_expands_user(tmp_path, monkeypatch):
    # Point HOME at tmp_path and resolve a ~-relative file.
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "note.txt"
    target.write_text("hi")
    resolved = resolve_path("~/note.txt")
    assert resolved.read_text() == "hi"


def test_resolve_path_finds_bare_name_in_downloads(tmp_path, monkeypatch):
    # A bare filename not in cwd but present in ~/Downloads must be found.
    monkeypatch.setenv("HOME", str(tmp_path))
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    doc = downloads / "LG2964.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    # cwd is somewhere without the file
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    resolved = resolve_path("LG2964.pdf")
    assert resolved == doc


def test_resolve_path_bare_name_prefers_cwd_over_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "dup.txt").write_text("downloads")
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "dup.txt").write_text("cwd")
    monkeypatch.chdir(workdir)
    resolved = resolve_path("dup.txt")
    assert resolved.read_text() == "cwd"


def test_resolve_path_missing_raises_with_searched_locations(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileResolutionError) as ei:
        resolve_path("nowhere.pdf")
    # The error must enumerate where it looked — resolution was attempted.
    assert ei.value.searched
    assert any("Downloads" in s for s in ei.value.searched)


def test_resolve_path_with_dir_component_does_not_autosearch(tmp_path, monkeypatch):
    # A path WITH a directory component is honored literally — we don't go
    # hunting in ~/Downloads for "sub/x.txt".
    monkeypatch.setenv("HOME", str(tmp_path))
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "x.txt").write_text("in downloads")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileResolutionError):
        resolve_path("sub/x.txt")


def test_resolve_path_empty_string_rejected(tmp_path, monkeypatch):
    # An empty/whitespace path must NOT silently resolve to the cwd.
    monkeypatch.chdir(tmp_path)
    for bad in ("", "   ", "\t"):
        with pytest.raises(FileResolutionError):
            resolve_path(bad)


def test_resolve_path_refuses_direct_protected(tmp_path, monkeypatch):
    # Resolving a protected path directly must raise ProtectedPathError — the
    # resolver won't even surface a secret's location.
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("KEY")
    with pytest.raises(ProtectedPathError):
        resolve_path("~/.ssh/id_rsa")


def test_resolve_path_bare_search_skips_protected(tmp_path, monkeypatch):
    # A bare-name search must not surface a protected file living in a
    # searched location (e.g. `credentials` / `.env` in ~/Downloads).
    monkeypatch.setenv("HOME", str(tmp_path))
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "credentials").write_text("SECRET")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    with pytest.raises(FileResolutionError):
        resolve_path("credentials")


# ---- read_document ----------------------------------------------------------


def test_read_document_plain_text(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# Title\nbody text")
    out = read_document(str(p))
    assert "body text" in out


def test_read_document_resolves_bare_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "report.txt").write_text("resolved via downloads")
    monkeypatch.chdir(tmp_path)
    assert "resolved via downloads" in read_document("report.txt")


def test_read_document_refuses_protected_path(tmp_path, monkeypatch):
    # A protected file that EXISTS must be refused (not read). Point HOME at
    # tmp and create a fake ~/.ssh/id_rsa so the protected-path rule fires.
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("SECRET KEY MATERIAL")
    with pytest.raises(ProtectedPathError):
        read_document("~/.ssh/id_rsa")


def test_read_document_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileResolutionError):
        read_document("ghost.pdf")


def test_read_document_empty_file_raises_honestly(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   \n  ")
    with pytest.raises(RuntimeError, match="no text"):
        read_document(str(p))


def test_read_document_truncates(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("A" * 10_000)
    out = read_document(str(p), max_chars=1000)
    assert "truncated to 1000 chars" in out
    assert len(out) < 2000


def test_read_document_directory_raises(tmp_path):
    with pytest.raises(IsADirectoryError):
        read_document(str(tmp_path))


# ---- PDF extraction path ----------------------------------------------------


def test_read_document_pdf_uses_pdftotext_when_present(tmp_path, monkeypatch):
    """When pdftotext is on PATH, _extract_pdf_text shells out to it. We stub
    both which() and subprocess.run so the test needs no real binary/PDF."""
    import forge.tools as tools

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake bytes")

    monkeypatch.setattr(tools._shutil, "which",
                        lambda name: "/usr/bin/pdftotext" if name == "pdftotext" else None)

    class _Proc:
        returncode = 0
        stdout = "extracted pdf body"
        stderr = ""

    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: _Proc())
    out = read_document(str(pdf))
    assert "extracted pdf body" in out


def test_read_document_pdf_no_backend_raises_actionable(tmp_path, monkeypatch):
    """No pdftotext and pypdf unavailable (and un-installable) → a clear,
    actionable error, not silence."""
    import forge.tools as tools

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    # No pdftotext CLI, and pypdf can neither be imported nor auto-installed.
    monkeypatch.setattr(tools._shutil, "which", lambda name: None)
    monkeypatch.setattr(tools, "_import_or_pip_install", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="no PDF backend"):
        read_document(str(pdf))


def test_import_or_pip_install_returns_existing_module():
    """A module that's already importable comes back without any install."""
    import forge.tools as tools
    mod = tools._import_or_pip_install("json")
    assert mod is not None
    assert mod.dumps({"a": 1}) == '{"a": 1}'


def test_import_or_pip_install_gives_up_gracefully(monkeypatch):
    """A truly missing package whose install fails returns None, never raises."""
    import forge.tools as tools

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "no matching distribution"

    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: _Fail())
    assert tools._import_or_pip_install("forge_nonexistent_pkg_xyz") is None


# ---- kernel wiring ----------------------------------------------------------


def test_new_primitives_are_injected_into_kernel_scope():
    from forge.tools import kernel_globals
    g = kernel_globals()
    assert "resolve_path" in g
    assert "read_document" in g
    assert "FileResolutionError" in g
    assert callable(g["read_document"])
