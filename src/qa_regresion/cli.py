"""Interfaz de línea de comandos.

    python -m qa_regresion run "/QA-Regresión <épica>" --profile config/project-profile.yaml --atlassian mcp
    python -m qa_regresion simulate
    python -m qa_regresion approve --run-dir <carpeta regression-plan> --message "Publicalo"

Toda ejecución `run` es dry-run: lee Jira/Confluence/documentos y termina en
AWAITING_PUBLICATION_APPROVAL sin escribir nada remoto.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from qa_regresion.config import load_profile
from qa_regresion.domain.enums import WorkflowPhase
from qa_regresion.publishers.approval import ApprovalRejected
from qa_regresion.workflows.artifacts import REVIEW_FILE, render_case_review, render_draft
from qa_regresion.workflows.regression_workflow import (
    PublicationDisabled,
    load_run,
    mark_draft_presented,
    mcp_writer_factory,
    publish_run,
    run_regression,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
EXIT = {WorkflowPhase.AWAITING_PUBLICATION_APPROVAL: 0, WorkflowPhase.AWAITING_EPIC_SELECTION: 2,
        WorkflowPhase.BLOCKED: 3, WorkflowPhase.FAILED: 1}


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _run(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    root = Path(args.project_root or profile.project.root or Path.cwd()).resolve()
    fixtures = Path(args.fixtures).resolve() if args.fixtures else None
    bundle = run_regression(profile, root, args.command, mode=args.atlassian, fixtures_dir=fixtures,
                            selected_key=args.select, run_id=args.run_id, pilot_size=getattr(args, "pilot", None))
    print(render_draft(bundle, profile.publication.approval_command))
    state = bundle.state
    if state.phase == WorkflowPhase.AWAITING_PUBLICATION_APPROVAL and bundle.output_dir is not None:
        mark_draft_presented(state, bundle.output_dir, profile)
    return EXIT.get(state.phase, 1)


def _simulate(args: argparse.Namespace) -> int:
    root = Path(args.project_root or PACKAGE_ROOT).resolve()
    fixtures = root / "tests" / "fixtures" / "simulated"
    workspace = root / ".simulacion" / "Formularios"
    for epic_dir in (fixtures / "Formularios").iterdir():
        target = workspace / epic_dir.name
        if args.clean and target.exists():
            shutil.rmtree(target)  # solo el espacio de simulación local, nunca Formulario/
        shutil.copytree(epic_dir, target, dirs_exist_ok=True)
    args.profile = args.profile or str(root / "config" / "simulated-profile.yaml")
    args.project_root = str(root)
    args.atlassian = "fixtures"
    args.fixtures = str(fixtures)
    args.command = args.command or "/QA-Regresión Portal de Devoluciones"
    return _run(args)


def _present(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    out = Path(args.run_dir)
    state, preview = load_run(out)
    review = render_case_review(preview)
    (out / REVIEW_FILE).write_text(review, encoding="utf-8")
    state = mark_draft_presented(state, out, profile)
    if len(preview.tests) <= 10:
        print(review)  # pocos casos: el borrador completo va en la consola
    else:
        print(f"Borrador {state.run_id} · Tarea: {preview.test_plan.summary} · Tests: {len(preview.tests)}")
        for test in preview.tests:
            print(f"  {test.summary[:80]} → historias: {', '.join(l.target for l in test.story_links) or '—'}")
    print(f"Borrador completo de los casos: {out / REVIEW_FILE}")
    print(f"Estado: {state.phase.value}. Revisar los casos; recién después enviar exactamente "
          f"'{profile.publication.approval_command}' para publicar.")
    return 0


def _approve(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    out = Path(args.run_dir)
    try:
        root = Path(profile.project.root or Path.cwd()).resolve()
        result = publish_run(out, args.message, profile,
                             writer_factory=mcp_writer_factory(profile, root) if profile.publication.enabled else None)
    except ApprovalRejected as exc:
        print(f"Publicación rechazada: {exc}")
        return 4
    except PublicationDisabled as exc:
        print(str(exc))
        return 5
    print(f"Resultado: {result.phase.value} · creados {len(result.created)} · actualizados {len(result.updated)} · "
          f"sin cambios {len(result.unchanged)} · enlaces {result.links_created}")
    if result.error:
        print(f"Error: {result.error} (reintentar con 'present' + '{profile.publication.approval_command}')")
    return 0 if result.phase == WorkflowPhase.PUBLISHED else 6


def _status(args: argparse.Namespace) -> int:
    state, preview = load_run(Path(args.run_dir))
    print(f"{state.run_id} · {state.epic_key} · {state.phase.value} · auditoría {state.audit_status} · "
          f"Tests en vista previa {len(preview.tests)} · escrituras remotas {state.remote_write_calls}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qa-regresion", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)

    run = sub.add_parser("run", help="/QA-Regresión <épica> en modo dry-run")
    run.add_argument("command", help='"/QA-Regresión <nombre de la épica>"')
    run.add_argument("--profile", default=None)
    run.add_argument("--project-root", default=None)
    run.add_argument("--atlassian", choices=["mcp", "fixtures"], default="mcp")
    run.add_argument("--fixtures", default=None)
    run.add_argument("--select", default=None, help="clave elegida cuando hay varias épicas")
    run.add_argument("--run-id", default=None)
    run.add_argument("--pilot", type=int, default=None,
                     help="publicar solo N Tests (uno por historia distinta) en la primera publicación")
    run.set_defaults(func=_run)

    sim = sub.add_parser("simulate", help="prueba simulada completa con fixtures")
    sim.add_argument("command", nargs="?", default=None)
    sim.add_argument("--profile", default=None)
    sim.add_argument("--project-root", default=None)
    sim.add_argument("--select", default=None)
    sim.add_argument("--run-id", default=None)
    sim.add_argument("--clean", action="store_true", help="recrea el espacio .simulacion desde los fixtures")
    sim.set_defaults(func=_simulate)

    present = sub.add_parser("present", help="volver a mostrar el borrador (habilita una nueva aprobación)")
    present.add_argument("--run-dir", required=True)
    present.add_argument("--profile", default=None)
    present.set_defaults(func=_present)

    approve = sub.add_parser("approve", help="procesa el mensaje de aprobación del usuario")
    approve.add_argument("--run-dir", required=True)
    approve.add_argument("--message", required=True)
    approve.add_argument("--profile", default=None)
    approve.set_defaults(func=_approve)

    status = sub.add_parser("status", help="estado de una ejecución")
    status.add_argument("--run-dir", required=True)
    status.set_defaults(func=_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and "qa-regresi" in argv[0].lower() and argv[0].lower().split("qa-regresi")[0].endswith(("/", "\\")):
        # Atajo: qa-regresion /QA-Regresión Nombre de la épica --atlassian mcp ...
        idx = next((i for i, a in enumerate(argv) if a.startswith("--")), len(argv))
        argv = ["run", " ".join(argv[:idx]), *argv[idx:]]
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"No se encontró una ejecución previa o un archivo requerido: {exc.filename}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
