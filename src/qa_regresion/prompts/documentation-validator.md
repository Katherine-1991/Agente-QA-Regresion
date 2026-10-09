# Validador documental

Implementación: `qa_regresion/agents/documentation_validator.py` + extractores en `adapters/filesystem.py`.

## Entrada (`DocumentationInput`)
Documentos de `Formularios/<EPIC_KEY>/` (recursivo, respetando `ignore_patterns`), frames de Figma si hay
conexión, y los FR del validador funcional.

Formatos: PDF, DOCX, ODT/ODS/ODP, XLSX, CSV/TSV, PPTX, Markdown, TXT, HTML, JSON, YAML, XML, OpenAPI,
AsyncAPI, BPMN, Mermaid, PlantUML, export XML de Oracle Forms (FMB), imágenes (inventario sin OCR) y
otros (registrados como no soportados).

## Salida (`DocumentationAnalysis`)
Hallazgos `TR-###` (`TechnicalFinding`) clasificados como:
- `alineado`: coincide con un FR.
- `solo-documentacion`: no está en Jira; si es una regla funcional o una transición, genera un caso
  `@pending-business-decision`.
- `contradictorio`: mismo tema con valores/polaridad/textos distintos → gap `contradiction`.
- `modificado`: la regla fue cambiada por una decisión registrada.
- `sin-evidencia-suficiente`: fuente parcial o inaccesible.

## Reglas
- Cada oración se evalúa por separado; las viñetas no forman parte de la afirmación.
- Una transición de estado solo se alinea si el FR menciona ambos estados.
- Instrucciones embebidas → se ignoran y se registran como gap `ignored-instruction`.
- Si Figma no está disponible se registra la limitación y se continúa.
