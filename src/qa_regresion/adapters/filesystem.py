"""Acceso seguro a la documentación local de la épica.

- Resuelve la carpeta de la épica sin permitir salir de la raíz documental (path traversal, symlinks).
- Inventaria recursivamente respetando `ignore_patterns` y el tamaño máximo.
- Extrae texto por formato con unidades localizadas (página, fila, diapositiva, línea, ítem).
Todo el contenido extraído se trata como dato no confiable.
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import html.parser
import io
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import yaml

from qa_regresion.config import ProjectProfile, expand_pattern
from qa_regresion.domain.enums import ReadStatus, SourceType
from qa_regresion.domain.models import ISSUE_KEY, SourceRecord
from qa_regresion.domain.text_analysis import normalize
from qa_regresion.domain.validation import detect_untrusted_instructions


class UnsafePathError(ValueError):
    """La ruta resultante quedaría fuera de la raíz documental permitida."""


@dataclass
class EpicFolder:
    expected_path: Path
    path: Path | None
    base: Path
    limitations: list[str] = field(default_factory=list)


@dataclass
class TextUnit:
    location: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedDocument:
    relative_path: str
    format: str
    read_status: ReadStatus
    units: list[TextUnit] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    size_bytes: int = 0
    sha256: str = ""


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def resolve_epic_folder(profile: ProjectProfile, project_root: Path, epic_key: str) -> EpicFolder:
    if not re.match(ISSUE_KEY, epic_key):
        raise UnsafePathError(f"clave de épica inválida para una ruta: {epic_key!r}")
    number = epic_key.split("-")[1]
    folder_name = epic_key if profile.documents.epic_folder_naming == "jira-key" else number
    pattern = profile.documents.root_pattern
    base_pattern = pattern.split("<epic-key>")[0].split("<epic-number>")[0]
    values = {"project-root": str(project_root), "epic-key": folder_name, "epic-number": number}
    base = Path(expand_pattern(base_pattern.rstrip("/\\") or ".", values))
    expected = Path(expand_pattern(pattern, values))
    if not _is_within(expected, base):
        raise UnsafePathError(f"la carpeta de la épica sale de la raíz documental: {expected}")
    if expected.is_symlink() and not _is_within(expected.resolve(), base):
        raise UnsafePathError(f"la carpeta de la épica es un enlace fuera de la raíz: {expected}")
    if expected.is_dir():
        return EpicFolder(expected_path=expected, path=expected, base=base)
    return EpicFolder(expected_path=expected, path=None, base=base, limitations=[
        f"No existe la carpeta documental esperada {expected}; se continúa solo con Jira y Confluence."])


def _match_ignore(rel_posix: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(rel_posix, pattern):
            return True
        if pattern.endswith("/**") and (rel_posix == pattern[:-3] or rel_posix.startswith(pattern[:-2])):
            return True
    return False


def inventory(folder: Path, ignore_patterns: list[str]) -> tuple[list[Path], list[str]]:
    """Lista archivos sin seguir enlaces. Devuelve (archivos, ignorados)."""
    files: list[Path] = []
    ignored: list[str] = []
    for root, dirs, names in os.walk(folder, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(folder).as_posix()
        keep = []
        for d in sorted(dirs):
            rel = f"{rel_root}/{d}" if rel_root != "." else d
            if _match_ignore(rel + "/", ignore_patterns) or _match_ignore(rel, ignore_patterns) or \
                    (root_path / d).is_symlink():
                ignored.append(rel + "/")
            else:
                keep.append(d)
        dirs[:] = keep
        for name in sorted(names):
            path = root_path / name
            rel = path.relative_to(folder).as_posix()
            if _match_ignore(rel, ignore_patterns):
                ignored.append(rel)
            elif path.is_symlink() and not _is_within(path, folder):
                ignored.append(rel + " (enlace fuera de la carpeta)")
            else:
                files.append(path)
    return files, ignored


def prepare_output_dir(output_dir: Path, epic_folder: Path | None, overwrite: bool, run_id: str) -> list[str]:
    """Crea la carpeta de salida. Sin `overwrite`, archiva la corrida previa en `_history/`."""
    notes: list[str] = []
    if epic_folder is not None and not _is_within(output_dir, epic_folder) and \
            output_dir.parent.resolve() != epic_folder.resolve():
        notes.append(f"La salida está fuera de la carpeta de la épica: {output_dir}")
    if output_dir.exists() and any(p.name != "_history" for p in output_dir.iterdir()):
        if overwrite:
            for p in output_dir.iterdir():
                if p.name == "_history":
                    continue
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            notes.append("Se reemplazó la corrida anterior (overwrite=true).")
        else:
            previous = "previa"
            state_file = output_dir / "workflow-state.json"
            if state_file.exists():
                try:
                    previous = json.loads(state_file.read_text(encoding="utf-8")).get("run_id", previous)
                except (json.JSONDecodeError, OSError):
                    pass
            target = output_dir / "_history" / f"{previous}"
            if target.exists():
                target = output_dir / "_history" / f"{previous}-{run_id}"
            target.mkdir(parents=True)
            for p in list(output_dir.iterdir()):
                if p.name != "_history":
                    shutil.move(str(p), str(target / p.name))
            notes.append(f"La corrida anterior se archivó en {target} (overwrite=false).")
    output_dir.mkdir(parents=True, exist_ok=True)
    return notes


# ---------------------------------------------------------------------- extracción


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _paragraph_units(text: str, prefix: str = "líneas") -> list[TextUnit]:
    units: list[TextUnit] = []
    buffer: list[str] = []
    start = 1
    in_code = False
    for n, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue  # el código (PL/SQL, JSON de ejemplo) no es una regla funcional
        if not stripped:
            if buffer:
                units.append(TextUnit(f"{prefix} {start}-{n - 1}", "\n".join(buffer)))
                buffer = []
            start = n + 1
            continue
        if not buffer:
            start = n
        buffer.append(stripped)
    if buffer:
        units.append(TextUnit(f"{prefix} {start}-{start + len(buffer) - 1}", "\n".join(buffer)))
    return units


_TRANSITION = re.compile(r"^\s*(\[\*\]|[\wÁÉÍÓÚÑáéíóúñ ]+?)\s*-+>\s*(\[\*\]|[\wÁÉÍÓÚÑáéíóúñ ]+?)\s*(?::\s*(.+))?$")


def _diagram_units(text: str, kind: str) -> list[TextUnit]:
    units = []
    for n, line in enumerate(text.splitlines(), start=1):
        m = _TRANSITION.match(line.strip())
        if m and m.group(1).strip() != "[*]" and m.group(2).strip() != "[*]":
            source, target, label = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()
            sentence = f"El estado «{source}» pasa a «{target}»" + (f" al {label}" if label else "")
            units.append(TextUnit(f"línea {n}", sentence, {"kind": "state-model", "from": source,
                                                           "to": target, "label": label, "diagram": kind}))
    return units


class _HtmlText(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self._skip = tag in ("script", "style")
        if tag in ("p", "li", "br", "tr", "h1", "h2", "h3", "h4", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _xml_texts(data: bytes, tag_suffixes: tuple[str, ...]) -> list[str]:
    root = ET.fromstring(data)
    out: list[str] = []
    for elem in root.iter():
        if elem.tag.split("}")[-1] in tag_suffixes:
            text = "".join(elem.itertext()).strip()
            if text:
                out.append(text)
    return out


def _docx_units(path: Path) -> list[TextUnit]:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    units = []
    for n, para in enumerate(root.iter(f"{ns}p"), start=1):
        text = "".join(t.text or "" for t in para.iter(f"{ns}t")).strip()
        if text:
            units.append(TextUnit(f"párrafo {n}", text))
    return units


def _odf_units(path: Path) -> list[TextUnit]:
    with zipfile.ZipFile(path) as z:
        texts = _xml_texts(z.read("content.xml"), ("p", "h"))
    return [TextUnit(f"párrafo {n}", t) for n, t in enumerate(texts, start=1)]


def _pptx_units(path: Path) -> list[TextUnit]:
    units = []
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for name in slides:
            number = re.findall(r"\d+", name)[-1]
            texts = _xml_texts(z.read(name), ("t",))
            if texts:
                units.append(TextUnit(f"diapositiva {number}", " ".join(texts)))
    return units


def _xlsx_rows(path: Path) -> list[tuple[str, list[str]]]:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rows: list[tuple[str, list[str]]] = []
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(f"{ns}t")) for si in root.iter(f"{ns}si")]
        sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        for sheet in sheets:
            root = ET.fromstring(z.read(sheet))
            sheet_no = re.findall(r"\d+", sheet)[-1]
            for row in root.iter(f"{ns}row"):
                values = []
                for cell in row.iter(f"{ns}c"):
                    v = cell.find(f"{ns}v")
                    if cell.get("t") == "s" and v is not None and v.text is not None:
                        values.append(shared[int(v.text)])
                    elif cell.get("t") == "inlineStr":
                        values.append("".join(t.text or "" for t in cell.iter(f"{ns}t")))
                    else:
                        values.append(v.text if v is not None and v.text else "")
                rows.append((f"hoja {sheet_no} fila {row.get('r')}", values))
    return rows


def _table_units(rows: list[tuple[str, list[str]]], profile: ProjectProfile) -> list[TextUnit]:
    if not rows:
        return []
    header = [h.strip() for h in rows[0][1]]
    norm_header = [normalize(h) for h in header]

    def col(candidates: list[str]) -> int | None:
        for c in candidates:
            if normalize(c) in norm_header:
                return norm_header.index(normalize(c))
        return None

    docs = profile.documents
    rule_col, feature_col = col(docs.tabular_rule_columns), col(docs.tabular_feature_columns)
    event_col, story_col = col(docs.tabular_event_columns), col(docs.tabular_story_columns)
    units = []
    for location, values in rows[1:]:
        cells = {header[i] if i < len(header) and header[i] else f"col{i + 1}": v.strip()
                 for i, v in enumerate(values) if v and v.strip()}
        if not cells:
            continue

        def get(idx: int | None) -> str:
            return values[idx].strip() if idx is not None and idx < len(values) and values[idx] else ""

        if rule_col is not None and get(rule_col):
            event = get(event_col)
            text = (f"{event}: " if event else "") + get(rule_col)
        else:
            text = "; ".join(f"{k}: {v}" for k, v in cells.items())
        meta = {"feature": get(feature_col) or None, "story": get(story_col) or None, "tabular": True}
        units.append(TextUnit(location, text, meta))
    return units


def _csv_rows(text: str, delimiter: str | None) -> list[tuple[str, list[str]]]:
    if delimiter is None:
        sample = text[:5000]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
        except csv.Error:
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = []
    for n, row in enumerate(reader, start=1):
        rows.append((f"fila {n}", row))
    return rows


def _structured_units(data: Any, label: str) -> tuple[list[TextUnit], str]:
    """JSON/YAML: detecta OpenAPI/AsyncAPI; si no, aplana textos."""
    units: list[TextUnit] = []
    if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
        for route, ops in (data.get("paths") or {}).items():
            for method, op in (ops or {}).items():
                if not isinstance(op, dict):
                    continue
                summary = op.get("summary") or op.get("description") or ""
                responses = ", ".join(str(code) for code in (op.get("responses") or {}))
                units.append(TextUnit(f"paths.{route}.{method}",
                                      f"Operación {method.upper()} {route}: {summary} (respuestas {responses})",
                                      {"kind": "interface-contract"}))
        for name, schema in ((data.get("components") or {}).get("schemas") or {}).items():
            required = (schema or {}).get("required") or []
            if required:
                units.append(TextUnit(f"components.schemas.{name}",
                                      f"El esquema {name} exige los campos obligatorios: {', '.join(required)}",
                                      {"kind": "interface-contract"}))
        return units, "openapi"
    if isinstance(data, dict) and "asyncapi" in data:
        for channel, spec in (data.get("channels") or {}).items():
            ops = [k for k in (spec or {}) if k in ("publish", "subscribe", "send", "receive")]
            units.append(TextUnit(f"channels.{channel}", f"Canal {channel}: {', '.join(ops) or 'definido'}",
                                  {"kind": "interface-contract"}))
        return units, "asyncapi"

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and len(node) >= 15:
            units.append(TextUnit(path or label, node))

    walk(data, "")
    return units, label


def _fmb_xml_units(root: ET.Element) -> list[TextUnit]:
    """Export XML de Oracle Forms: ítems obligatorios/longitudes por bloque y mensajes del legado."""
    units: list[TextUnit] = []

    def local(tag: str) -> str:
        return tag.split("}")[-1]

    for block in root.iter():
        if local(block.tag) != "Block":
            continue
        name = block.get("Name", "?")
        required, lengths = [], []
        for item in block:
            if local(item.tag) != "Item":
                continue
            item_name = item.get("Name", "")
            if item_name.startswith(("CG$", "CGC$", "CGU$")):
                continue
            label = item.get("Prompt") or item_name
            if item.get("Required") == "true":
                required.append(label)
            if item.get("MaximumLength") and item.get("DatabaseItem") != "false":
                lengths.append(f"{label}={item.get('MaximumLength')}")
        if required or lengths:
            parts = []
            if required:
                parts.append(f"ítems obligatorios: {', '.join(required)}")
            if lengths:
                parts.append(f"longitudes máximas: {', '.join(lengths[:15])}")
            units.append(TextUnit(f"bloque {name}", f"Bloque {name} del legado — " + "; ".join(parts),
                                  {"kind": "technical-constraint"}))
    seen: set[str] = set()
    message_re = re.compile(r"(?:MENSAJE_ERROR|MESSAGE|MENSAJE)\s*\(\s*'([^']{8,200})'", re.IGNORECASE)
    for trig in root.iter():
        if local(trig.tag) != "Trigger":
            continue
        code = trig.get("TriggerText", "") or ""
        for m in message_re.finditer(code.replace("&#10;", "\n")):
            msg = m.group(1).strip()
            if msg in seen:
                continue
            seen.add(msg)
            units.append(TextUnit(f"trigger {trig.get('Name', '?')}",
                                  f"El legado muestra el mensaje '{msg}'", {"kind": "technical-constraint"}))
    return units


def _bpmn_units(root: ET.Element) -> list[TextUnit]:
    units = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag in ("task", "userTask", "serviceTask", "exclusiveGateway", "startEvent", "endEvent") and elem.get("name"):
            units.append(TextUnit(f"{tag} {elem.get('id')}", f"Proceso: {tag} «{elem.get('name')}»",
                                  {"kind": "state-model"}))
        if tag == "sequenceFlow" and elem.get("name"):
            units.append(TextUnit(f"flujo {elem.get('id')}", f"Flujo {elem.get('sourceRef')} → "
                                  f"{elem.get('targetRef')}: {elem.get('name')}", {"kind": "state-model"}))
    return units


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def extract_document(path: Path, folder: Path, profile: ProjectProfile) -> ExtractedDocument:
    rel = path.relative_to(folder).as_posix()
    ext = path.suffix.lower()
    size = path.stat().st_size
    doc = ExtractedDocument(relative_path=rel, format=ext.lstrip(".") or "sin-extension",
                            read_status=ReadStatus.COMPLETE, size_bytes=size)
    if size > profile.documents.max_file_mb * 1024 * 1024:
        doc.read_status = ReadStatus.INACCESSIBLE
        doc.limitations.append(f"supera el tamaño máximo de {profile.documents.max_file_mb} MB")
        return doc
    try:
        raw = path.read_bytes()
        doc.sha256 = hashlib.sha256(raw).hexdigest()[:16]
        if ext in (".md", ".markdown", ".txt", ".rst", ".log"):
            text = _read_text(path)
            doc.units = _paragraph_units(text)
            for block in re.findall(r"```mermaid\s*\n(.*?)```", text, flags=re.DOTALL):
                doc.units.extend(_diagram_units(block, "mermaid"))
        elif ext in (".mmd", ".mermaid", ".puml", ".plantuml", ".pu"):
            doc.format = "mermaid" if ext in (".mmd", ".mermaid") else "plantuml"
            doc.units = _diagram_units(_read_text(path), doc.format)
            if not doc.units:
                doc.read_status = ReadStatus.PARTIAL
                doc.limitations.append("diagrama sin transiciones reconocibles")
        elif ext in (".html", ".htm"):
            parser = _HtmlText()
            parser.feed(_read_text(path))
            doc.units = _paragraph_units("".join(parser.parts))
        elif ext in (".csv", ".tsv"):
            rows = _csv_rows(_read_text(path), "\t" if ext == ".tsv" else None)
            doc.units = _table_units(rows, profile)
        elif ext == ".json":
            doc.units, doc.format = _structured_units(json.loads(_read_text(path)), "json")
        elif ext in (".yaml", ".yml"):
            doc.units, fmt = _structured_units(yaml.safe_load(_read_text(path)), "yaml")
            doc.format = fmt
        elif ext in (".xml", ".bpmn"):
            root = ET.fromstring(raw)
            tag = root.tag.split("}")[-1]
            if tag == "Module" and "oracle.com/Forms" in root.tag:
                doc.format = "fmb-xml"
                doc.units = _fmb_xml_units(root)
            elif tag == "definitions" or ext == ".bpmn":
                doc.format = "bpmn"
                doc.units = _bpmn_units(root)
            else:
                texts = [t.strip() for t in root.itertext() if t and len(t.strip()) >= 15]
                doc.units = [TextUnit(f"nodo {i}", t) for i, t in enumerate(texts, start=1)]
        elif ext == ".fmb":
            doc.read_status = ReadStatus.PARTIAL
            sibling = list(path.parent.glob(path.stem + "*_fmb.xml")) + list(path.parent.glob(path.stem + "*.xml"))
            doc.limitations.append("FMB binario: solo inventario" +
                                   (f"; se analiza el export XML {sibling[0].name}" if sibling else
                                    "; pedir el export XML para analizarlo"))
        elif ext == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                doc.read_status = ReadStatus.INACCESSIBLE
                doc.limitations.append("pypdf no instalado: PDF no analizado")
                return doc
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:
                doc.read_status = ReadStatus.INACCESSIBLE
                doc.limitations.append("PDF cifrado")
                return doc
            for n, page in enumerate(reader.pages, start=1):
                for unit in _paragraph_units(page.extract_text() or "", prefix=f"página {n} líneas"):
                    doc.units.append(unit)
            if not doc.units:
                doc.read_status = ReadStatus.PARTIAL
                doc.limitations.append("PDF sin texto extraíble (posible escaneo, requiere OCR)")
        elif ext == ".docx":
            doc.units = _docx_units(path)
        elif ext in (".odt", ".odp", ".ods"):
            doc.units = _odf_units(path)
        elif ext == ".xlsx":
            doc.units = _table_units(_xlsx_rows(path), profile)
        elif ext == ".pptx":
            doc.units = _pptx_units(path)
        elif ext == ".svg":
            doc.format = "svg"
            doc.units = [TextUnit(f"texto {i}", t) for i, t in enumerate(_xml_texts(raw, ("text", "tspan")), 1)]
            doc.read_status = ReadStatus.PARTIAL
            doc.limitations.append("imagen vectorial: solo se leen los textos")
        elif ext in IMAGE_EXT:
            doc.format = "imagen"
            doc.read_status = ReadStatus.PARTIAL
            doc.limitations.append("imagen: inventariada sin OCR; requiere revisión visual")
        else:
            doc.read_status = ReadStatus.INACCESSIBLE
            doc.limitations.append(f"formato no soportado ({ext or 'sin extensión'})")
    except (zipfile.BadZipFile, ET.ParseError, json.JSONDecodeError, yaml.YAMLError, KeyError, ValueError,
            OSError, UnicodeError) as exc:
        doc.read_status = ReadStatus.INACCESSIBLE
        doc.units = []
        doc.limitations.append(f"no se pudo leer: {type(exc).__name__}: {str(exc)[:120]}")
    except Exception as exc:  # noqa: BLE001 - un documento roto no corta la corrida
        doc.read_status = ReadStatus.INACCESSIBLE
        doc.units = []
        doc.limitations.append(f"error de lectura: {type(exc).__name__}: {str(exc)[:120]}")
    return doc


def document_to_record(doc: ExtractedDocument) -> SourceRecord:
    text = "\n".join(u.text for u in doc.units)
    untrusted = detect_untrusted_instructions(text)
    return SourceRecord(
        source_id=f"doc:{doc.relative_path}", source_type=SourceType.DOCUMENT, title=doc.relative_path,
        location=doc.relative_path, read_status=doc.read_status, format=doc.format, size_bytes=doc.size_bytes,
        content_hash=doc.sha256 or None, text=text,
        fields={"units": [{"location": u.location, "text": u.text, "meta": u.meta} for u in doc.units]},
        untrusted_instructions=untrusted, limitations=doc.limitations)


def load_documents(folder: Path | None, profile: ProjectProfile) -> tuple[list[SourceRecord], list[str]]:
    if folder is None:
        return [], []
    files, ignored = inventory(folder, profile.documents.ignore_patterns)
    records = [document_to_record(extract_document(p, folder, profile)) for p in files]
    return records, ignored
