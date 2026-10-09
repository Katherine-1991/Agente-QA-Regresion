# Auditor QA independiente

Implementación: `qa_regresion/agents/independent_auditor.py`.

No puede crear ni reescribir casos: recibe una copia y el orquestador verifica con una huella que el diseño
no cambió durante la auditoría.

## Revisa
Requisitos y criterios cubiertos, riesgos (defectos históricos, límites), trazabilidad y evidencia, fuentes en
cuarentena, duplicados, calidad Gherkin, datos reproducibles, prioridades, suites, automatización, relaciones
con historias, casos pendientes y bloqueados, y preparación para publicación.

## Salida requerida
```yaml
audit_status: PASS | PASS_WITH_WARNINGS | FAIL
publication_ready: true | false
critical_findings: []
major_findings: []
minor_findings: []
coverage:
  requirements:        {covered: 0, total: 0}
  acceptance_criteria: {covered: 0, total: 0}
  traceable_test_cases: {covered: 0, total: 0}
```
- Críticos o mayores → `FAIL` (el orquestador deriva cada hallazgo a `responsible_agent`).
- Solo menores → `PASS_WITH_WARNINGS`.
- `FAIL` nunca es `publication_ready`.
