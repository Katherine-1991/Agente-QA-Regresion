# Agente QA-Regresión

Sistema multiagente de QA que analiza Jira, Confluence y documentación funcional para generar planes de
regresión trazables, casos Gherkin automatizables y su publicación controlada en Jira mediante auditoría
independiente.

Arma el **plan de regresión funcional** de una épica, lo audita con un agente independiente y deja un
**borrador de los casos para revisión humana**. Nada se publica hasta que la persona revisa el borrador y
envía, como mensaje independiente, exactamente `Publicalo`.

> Este repositorio no incluye credenciales, tokens, URLs de instancias ni configuración de conexión a sistemas
> externos. La conexión a Jira se configura localmente en `config/project-profile.yaml` (ignorado por git) y las
> credenciales las administra el servidor MCP de Atlassian que cada persona tenga instalado.

## Uso

```powershell
# Instalación (una vez)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mcp,pdf,dev]"

# Prueba simulada completa con fixtures (sin red)
.\.venv\Scripts\python.exe -m qa_regresion simulate --clean

# Pruebas automatizadas
.\.venv\Scripts\python.exe -m pytest
```

### Épica real (solo lectura)

1. Copiar el perfil de ejemplo y completarlo con la instancia propia (este archivo no se versiona):
   `copy src\qa_regresion\config\project-profile.example.yaml config\project-profile.yaml`
2. En `atlassian.mcp` indicar cómo lanzar el MCP local de Atlassian (por ejemplo, un `.mcp.json` propio). Las
   credenciales quedan en la configuración de ese servidor, nunca en este repositorio.
3. Ejecutar:

```powershell
.\.venv\Scripts\python.exe -m qa_regresion run "/QA-Regresión PROJ-123" --profile config/project-profile.yaml --atlassian mcp
# varias épicas coinciden con el nombre → elegir
.\.venv\Scripts\python.exe -m qa_regresion run "/QA-Regresión Nombre de la épica" --profile config/project-profile.yaml --select PROJ-123
# primera publicación acotada a N casos (uno por historia)
.\.venv\Scripts\python.exe -m qa_regresion run "/QA-Regresión PROJ-123" --profile config/project-profile.yaml --pilot 3
# volver a mostrar el borrador / estado
.\.venv\Scripts\python.exe -m qa_regresion present --run-dir "<carpeta regression-plan>" --profile config/project-profile.yaml
.\.venv\Scripts\python.exe -m qa_regresion status --run-dir "<carpeta regression-plan>"
# publicar: solo después de revisar borrador-casos-a-publicar.md
.\.venv\Scripts\python.exe -m qa_regresion approve --run-dir "<carpeta regression-plan>" --message "Publicalo" --profile config/project-profile.yaml
```

Desde Claude Code: `/QA-Regresión <épica>` (`.claude/commands/QA-Regresión.md`).

Códigos de salida de `run`: 0 `AWAITING_PUBLICATION_APPROVAL`, 2 selección de épica pendiente, 3 `BLOCKED`
(auditoría FAIL tras 2 correcciones), 1 `FAILED`. `approve`: 4 rechazada, 5 publicación deshabilitada.

## Arquitectura

Máquina de estados explícita (`workflows/state_machine.py`), sin framework multiagente:

```
RECEIVED → RESOLVING_EPIC → [AWAITING_EPIC_SELECTION] → COLLECTING_SOURCES → VALIDATING_FUNCTIONAL
→ VALIDATING_DOCUMENTATION → CONSOLIDATING → DESIGNING_CASES → AUDITING ⇄ REPAIRING (máx. 2)
→ GENERATING_DRAFT → AWAITING_PUBLICATION_APPROVAL | BLOCKED
AWAITING_PUBLICATION_APPROVAL → (revisión + Publicalo) → PUBLISHING → PUBLISHED | PARTIALLY_PUBLISHED
```

| Pieza | Archivo | Rol |
|---|---|---|
| Líder QA / orquestador | `agents/orchestrator.py` | Comando, épica, fuentes, consolidación, matriz de impacto, ciclos de corrección, borrador |
| Validador funcional | `agents/functional_validator.py` | FR-### atómicos con evidencia; criterios de aceptación; decisiones en comentarios; defectos; no inventa |
| Validador documental | `agents/documentation_validator.py` | TR-### clasificados (alineado, solo-documentacion, contradictorio, modificado, sin-evidencia-suficiente) |
| Diseñador Gherkin | `agents/gherkin_designer.py` | Un caso por criterio de aceptación, decisiones, dedupe, TC-### estables, datos, suites, `.feature` |
| Auditor independiente | `agents/independent_auditor.py` | Solo lectura; `PASS / PASS_WITH_WARNINGS / FAIL` + cobertura |
| Adaptador Atlassian | `adapters/atlassian_mcp.py` | Cliente MCP genérico, guarda de solo lectura, paginación, reintentos, dedupe, Test Plan de referencia |
| Simulador | `adapters/fixture_gateway.py` | Mismas herramientas MCP sobre fixtures (mini JQL/CQL) |
| Documentos | `adapters/filesystem.py` | Carpeta segura, inventario, extractores por formato |
| Figma | `adapters/figma.py` | Lectura opcional; sin conexión registra la limitación |
| Publicación | `publishers/approval.py`, `publishers/jira_publisher.py` | Candado `Publicalo`, vista previa, borrador de revisión, publicación idempotente |
| Modelos | `domain/models.py`, `domain/contracts.py` | Pydantic estricto (`extra="forbid"`); intercambios revalidados |
| Prompts / contratos | `prompts/*.md` | Contrato de cada agente |
| Perfiles | `src/qa_regresion/config/project-profile.example.yaml`, `config/simulated-profile.yaml` | Tipos, campos, rutas, MCP, publicación |

Los agentes son deterministas (reglas explicables sobre texto en español). Cada intercambio se valida con su
esquema (`validate_exchange`). El auditor recibe una copia y el orquestador verifica con una huella que no
cambió nada.

## Seguridad

- Toda ejecución `run` es dry-run. En modo MCP el servidor se lanza con `READ_ONLY_MODE=true` y además
  `ReadOnlyGuard` solo deja pasar herramientas de lectura (cualquier `create/update/link/...` se bloquea).
- Jira, Confluence, Figma y documentos son contenido no confiable: las instrucciones embebidas se detectan,
  se ignoran y quedan en `gaps.md` / `qa-validation-report.md` sin ningún efecto.
- La aprobación solo vale si: llega por el canal del usuario, es exactamente `Publicalo`, es posterior al
  borrador mostrado, el contenido y el destino no cambiaron (huellas recalculadas desde el archivo) y no se
  usó antes. Además el perfil debe tener `publication.enabled: true`.
- Sin secretos ni configuración de conexión en el repositorio (ver `.gitignore`).

## Artefactos (`<carpeta de la épica>/regression-plan/`)

`test-plan-regresion.md`, `trazabilidad.csv`, `matriz-impacto.csv`, `catalogo-datos-prueba.md`, `gaps.md`,
`source-manifest.json`, `qa-validation-report.md`, `publication-preview.json`, `borrador-casos-a-publicar.md`,
`features/*.feature`, más `workflow-state.json`, `tc-registry.json` (IDs estables) y `analisis.json`. Sin
`overwrite`, la corrida anterior se archiva en `regression-plan/_history/<run_id>/`.

## Publicación en Jira

Relaciones configurables en el perfil, verificadas en cada corrida contra un Test Plan de referencia
(`atlassian.reference_issue`, solo lectura; el borrador indica si el perfil coincide):

| Relación | Opciones del perfil |
|---|---|
| Test Plan → épica | Tarea **hija** de la épica (campo padre) |
| Test → Test Plan | Cada Test es **subtarea** de la Tarea (`test_plan_association: parent`) o enlace |
| Test → historias | Enlace **Relates**, uno por historia cubierta |
| Componentes / campos | Los de la épica (`inherit_epic_components`, `inherit_epic_fields`) en la Tarea y en cada Test |

- **Antes de publicar** cada corrida genera `borrador-casos-a-publicar.md`: cada Test tal como se crearía (título,
  precondiciones, Gherkin, trazabilidad, historias, componentes, campos heredados). Se revisa y recién después se
  envía `Publicalo`. Con `run --pilot N` se publica primero una muestra de N casos.
- Casos: con `design.case_sources: [acceptance-criteria]` sale **un caso por criterio de aceptación**, con su
  Dado/Cuando/Entonces; las demás reglas quedan listadas en `gaps.md` sin caso propio.
- Tarea `Test Plan - Regresión | <épica>` con alcance, riesgos, métricas, auditoría, identificador de ejecución,
  casos asociados y artefactos adjuntos. Un Test por caso (`TC-### | título`).
- Idempotencia: busca lo ya publicado por relaciones (o por etiquetas técnicas) y compara un hash de contenido →
  `new / modified / unchanged / blocked / pending / potentially-obsolete`. Nunca elimina ni cierra; los Tests de
  otros planes no se tocan. Un diario (`publication-journal.json`) permite retomar publicaciones parciales.

## Limitaciones conocidas

- La extracción es heurística: los criterios sin acción explícita generan un paso `Cuando` de verificación
  ("el usuario accede a la funcionalidad …"). El borrador siempre requiere revisión humana.
- Si el MCP de Atlassian solo expone herramientas de Jira, Confluence queda registrado como no disponible.
- Figma no está conectado al proceso Python; las imágenes se inventarían sin OCR.
- Los datos de prueba indican qué preparar; no incluyen valores concretos del ambiente.
