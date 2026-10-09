# Diseñador Gherkin

Implementación: `qa_regresion/agents/gherkin_designer.py`.

## Entrada (`DesignInput`)
FR consolidados (incluye reglas solo documentales promovidas a FR pendientes), TR, gaps, defectos y el
registro de IDs estables de corridas anteriores (`tc-registry.json`).

## Salida (`GherkinDesign`)
- `candidates`: `TestCaseCandidate` con decisión `accepted | rewritten | merged | split | blocked | rejected`.
- `cases`: `GherkinTestCase` con ID estable `TC-###`.
- `datasets`: `TestDataSet` (`explicit | fixture | to-provision`).
- `features`: un `.feature` por funcionalidad (`# language: es`).

## Reglas de diseño
- Un escenario prueba un comportamiento, es independiente, tiene precondiciones reproducibles, datos
  explícitos o a aprovisionar, una sola acción principal (`Cuando`) y resultados observables (`Entonces`).
- Sin selectores ni detalles internos (se reescriben).
- Por categoría: rol → autorizado y no autorizado; validación → incumplida y cumplida; límite → esquema con
  ejemplos en el borde; transición → estado inicial y final; defecto → `@defect-regression`.
- Regla solo documental o en contradicción → `@pending-business-decision` con motivo.
- Evidencia inaccesible → `blocked` (no se publica).
- Etiquetas: tipo (`@positive`, `@negative`, `@boundary`, `@role`, `@integration`, `@state-transition`,
  `@defect-regression`), suite (`@smoke` ⊂ `@critical-path` ⊂ `@full-regression`), automatización
  (`@automation-candidate` | `@manual-only`) y `@pending-business-decision`.
- Deduplicar por firma de pasos; los casos fusionados conservan la trazabilidad de todos sus FR.
