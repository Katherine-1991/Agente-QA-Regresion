from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from qa_regresion.adapters.filesystem import (
    UnsafePathError,
    extract_document,
    inventory,
    load_documents,
    prepare_output_dir,
    resolve_epic_folder,
)
from qa_regresion.domain.enums import ReadStatus


def test_safe_folder_resolves_inside_root(profile, workspace):
    folder = resolve_epic_folder(profile, workspace, "QAS-100")
    assert folder.path is not None and folder.path.name == "QAS-100"
    assert folder.limitations == []


@pytest.mark.parametrize("bad_key", ["../QAS-100", "QAS-100/../../etc", "..\\x", "QAS-1 00", "", "qas_100"])
def test_unsafe_epic_keys_are_rejected(profile, workspace, bad_key):
    with pytest.raises(UnsafePathError):
        resolve_epic_folder(profile, workspace, bad_key)


def test_numeric_folder_naming(profile, workspace):
    profile.documents.epic_folder_naming = "numeric"
    folder = resolve_epic_folder(profile, workspace, "QAS-100")
    assert folder.expected_path.name == "100"
    assert folder.path is None and "No existe la carpeta" in folder.limitations[0]


def test_missing_folder_reports_expected_path(profile, tmp_path):
    folder = resolve_epic_folder(profile, tmp_path, "QAS-777")
    assert folder.path is None
    assert str(folder.expected_path) in folder.limitations[0]


def test_ignore_patterns(tmp_path):
    (tmp_path / "regression-plan").mkdir()
    (tmp_path / "regression-plan" / "x.md").write_text("generado", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    (tmp_path / "doc.md").write_text("El campo es obligatorio.", encoding="utf-8")
    files, ignored = inventory(tmp_path, [".git/**", "regression-plan/**"])
    assert [f.name for f in files] == ["doc.md"]
    assert "regression-plan/" in ignored and ".git/" in ignored


def test_inaccessible_documents_are_marked(profile, tmp_path):
    (tmp_path / "roto.pdf").write_bytes(b"%PDF-1.4 garbage")
    (tmp_path / "roto.docx").write_bytes(b"no es un zip")
    (tmp_path / "desconocido.xyz").write_bytes(b"\x00\x01")
    (tmp_path / "enorme.txt").write_text("x" * 2048, encoding="utf-8")
    profile.documents.max_file_mb = 0.001
    statuses = {p.name: extract_document(p, tmp_path, profile).read_status for p in tmp_path.iterdir()}
    assert statuses == {"roto.pdf": ReadStatus.INACCESSIBLE, "roto.docx": ReadStatus.INACCESSIBLE,
                        "desconocido.xyz": ReadStatus.INACCESSIBLE, "enorme.txt": ReadStatus.INACCESSIBLE}


def _zip(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)


def test_office_formats_are_extracted(profile, tmp_path):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    _zip(tmp_path / "a.docx", {"word/document.xml":
         f'<w:document xmlns:w="{w}"><w:body><w:p><w:r><w:t>El importe es obligatorio.</w:t></w:r></w:p>'
         f'</w:body></w:document>'})
    s = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    _zip(tmp_path / "b.xlsx", {
        "xl/sharedStrings.xml": f'<sst xmlns="{s}"><si><t>Descripción</t></si><si><t>El CUIT es obligatorio</t></si></sst>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{s}"><sheetData><row r="1"><c t="s"><v>0</v></c></row>'
                                    f'<row r="2"><c t="s"><v>1</v></c></row></sheetData></worksheet>'})
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    _zip(tmp_path / "c.pptx", {"ppt/slides/slide1.xml": f'<p:sld xmlns:p="x" xmlns:a="{a}"><a:t>El rol Supervisor aprueba</a:t></p:sld>'})
    _zip(tmp_path / "d.odt", {"content.xml": '<office:document-content xmlns:office="o" '
                                             'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                                             '<text:p>El monto máximo es 10.</text:p></office:document-content>'})
    texts = {p.name: " ".join(u.text for u in extract_document(p, tmp_path, profile).units)
             for p in tmp_path.iterdir()}
    assert "obligatorio" in texts["a.docx"]
    assert "El CUIT es obligatorio" in texts["b.xlsx"]
    assert "Supervisor" in texts["c.pptx"]
    assert "máximo" in texts["d.odt"]


def test_structured_and_diagram_formats(profile, tmp_path):
    (tmp_path / "api.json").write_text('{"openapi": "3.0.0", "paths": {"/x": {"get": {"summary": "lista"}}}}',
                                       encoding="utf-8")
    (tmp_path / "async.yaml").write_text("asyncapi: 2.0.0\nchannels:\n  pagos:\n    publish: {}\n", encoding="utf-8")
    (tmp_path / "flujo.puml").write_text("@startuml\nBorrador --> Enviado : enviar\n@enduml\n", encoding="utf-8")
    (tmp_path / "proc.bpmn").write_text('<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">'
                                        '<process><userTask id="t1" name="Aprobar"/></process></definitions>',
                                        encoding="utf-8")
    (tmp_path / "p.html").write_text("<html><body><p>El campo es obligatorio.</p><script>x()</script></body></html>",
                                     encoding="utf-8")
    (tmp_path / "forma_fmb.xml").write_text(
        '<Module xmlns="http://xmlns.oracle.com/Forms"><FormModule Name="F"><Block Name="B">'
        '<Item Name="CUIT" Prompt="CUIT" Required="true" MaximumLength="11"/></Block>'
        '<Trigger Name="WHEN-NEW-FORM-INSTANCE" TriggerText="MENSAJE_ERROR(\'No está autorizado para entrar\');"/>'
        '</FormModule></Module>', encoding="utf-8")
    docs = {p.name: extract_document(p, tmp_path, profile) for p in tmp_path.iterdir()}
    assert docs["api.json"].format == "openapi" and docs["api.json"].units
    assert docs["async.yaml"].format == "asyncapi"
    assert docs["flujo.puml"].units[0].meta["to"] == "Enviado"
    assert docs["proc.bpmn"].units[0].text.endswith("«Aprobar»")
    assert "script" not in " ".join(u.text for u in docs["p.html"].units)
    fmb = " ".join(u.text for u in docs["forma_fmb.xml"].units)
    assert docs["forma_fmb.xml"].format == "fmb-xml" and "CUIT" in fmb and "No está autorizado" in fmb


def test_images_and_binary_fmb_are_partial(profile, workspace):
    folder = workspace / ".simulacion" / "Formularios" / "QAS-100"
    (folder / "forma.fmb").write_bytes(b"\x00binario")
    records, _ = load_documents(folder, profile)
    by_name = {r.location: r for r in records}
    assert by_name["pantalla-solicitud.png"].read_status == ReadStatus.PARTIAL
    assert by_name["forma.fmb"].read_status == ReadStatus.PARTIAL
    assert by_name["anexo-corrupto.pdf"].read_status == ReadStatus.INACCESSIBLE


def test_output_dir_archives_previous_run(tmp_path):
    out = tmp_path / "regression-plan"
    out.mkdir()
    (out / "workflow-state.json").write_text('{"run_id": "run-old"}', encoding="utf-8")
    (out / "gaps.md").write_text("viejo", encoding="utf-8")
    notes = prepare_output_dir(out, tmp_path, overwrite=False, run_id="run-new")
    assert (out / "_history" / "run-old" / "gaps.md").read_text(encoding="utf-8") == "viejo"
    assert not (out / "gaps.md").exists()
    assert "archivó" in notes[0]
