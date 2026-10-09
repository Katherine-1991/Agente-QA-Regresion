"""Perfil de proyecto: todo lo que depende de la instancia de Jira/Confluence vive acá, no en el código."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qa_regresion.domain.text_analysis import normalize

EXAMPLE_PROFILE = Path(__file__).with_name("project-profile.example.yaml")


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCfg(_Cfg):
    name: str = "proyecto-ejemplo"
    root: str | None = None


class RetryCfg(_Cfg):
    max_attempts: int = Field(default=3, ge=1, le=6)
    backoff_seconds: float = Field(default=1.0, ge=0)


class McpToolNames(_Cfg):
    jira_search: str = "jira_search"
    jira_get_issue: str = "jira_get_issue"
    confluence_search: str = "confluence_search"
    confluence_get_page: str = "confluence_get_page"
    jira_create_issue: str = "jira_create_issue"
    jira_update_issue: str = "jira_update_issue"
    jira_create_issue_link: str = "jira_create_issue_link"


class McpCfg(_Cfg):
    config_file: str | None = None
    server_name: str = "atlassian"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    read_only_env: dict[str, str] = Field(default_factory=lambda: {"READ_ONLY_MODE": "true"})
    timeout_seconds: float = 90
    tools: McpToolNames = Field(default_factory=McpToolNames)
    read_tools: list[str] = Field(default_factory=lambda: [
        "jira_search", "jira_get_issue", "jira_search_fields", "jira_get_link_types",
        "jira_get_project_issue_types", "confluence_search", "confluence_get_page",
    ])


class IssueTypeAliases(_Cfg):
    epic: list[str] = Field(default_factory=lambda: ["Epic", "Épica"])
    story: list[str] = Field(default_factory=lambda: ["Story", "Historia de usuario", "Historia"])
    improvement: list[str] = Field(default_factory=lambda: ["Improvement", "Mejora"])
    out_of_scope: list[str] = Field(default_factory=lambda: ["Out of scope", "Fuera de alcance"])
    incident: list[str] = Field(default_factory=lambda: ["Bug", "Incident", "Incidencia"])
    test_plan: list[str] = Field(default_factory=lambda: ["Task", "Tarea"])
    test_case: list[str] = Field(default_factory=lambda: ["Test"])


class AtlassianCfg(_Cfg):
    base_url: str | None = None
    jira_projects: list[str] = Field(default_factory=list)
    confluence_spaces: list[str] = Field(default_factory=list)
    issue_type_aliases: IssueTypeAliases = Field(default_factory=IssueTypeAliases)
    acceptance_criteria_fields: list[str] = Field(default_factory=list)
    acceptance_criteria_headings: list[str] = Field(default_factory=lambda: [
        "Criterios de aceptación", "Criterios de aceptacion", "Acceptance criteria", "CA:",
    ])
    out_of_scope_headings: list[str] = Field(default_factory=lambda: ["Fuera de alcance", "Out of scope"])
    business_rule_headings: list[str] = Field(default_factory=lambda: [
        "Reglas de negocio", "Reglas funcionales", "Validaciones", "Business rules"])
    decision_markers: list[str] = Field(default_factory=lambda: [
        "decisión", "decision", "se decide", "se decidió", "acordamos", "se acordó", "definimos",
        "queda definido", "confirmado por negocio", "negocio confirma",
    ])
    epic_child_jql: list[str] = Field(default_factory=lambda: ['parent = {epic_key}', '"Epic Link" = {epic_key}'])
    related_text_search: bool = True
    page_size: int = Field(default=50, ge=1, le=100)
    max_pages: int = Field(default=20, ge=1)
    comment_limit: int = Field(default=50, ge=0, le=100)
    retry: RetryCfg = Field(default_factory=RetryCfg)
    reference_issue: str | None = None
    mcp: McpCfg = Field(default_factory=McpCfg)


class DocumentsCfg(_Cfg):
    root_pattern: str = "<project-root>/Formularios/<epic-key>"
    epic_folder_naming: str = Field(default="jira-key", pattern=r"^(jira-key|numeric)$")
    ignore_patterns: list[str] = Field(default_factory=lambda: [".git/**", "regression-plan/**"])
    max_file_mb: float = 25
    max_doc_only_cases: int = 25
    tabular_rule_columns: list[str] = Field(default_factory=lambda: [
        "Descripción", "Descripcion", "Regla", "Rule", "Description", "Criterio",
    ])
    tabular_feature_columns: list[str] = Field(default_factory=lambda: ["Funcionalidad", "Feature", "Grupo"])
    tabular_event_columns: list[str] = Field(default_factory=lambda: ["Evento de ejecución", "Evento", "Event"])
    tabular_story_columns: list[str] = Field(default_factory=lambda: ["User Story", "Historia", "HU", "Story"])


class FigmaCfg(_Cfg):
    enabled: bool = False
    file_keys: list[str] = Field(default_factory=list)


CASE_SOURCES = ("acceptance-criteria", "business-rules", "decisions", "defects", "documentation")


class DesignCfg(_Cfg):
    """De qué requisitos salen casos. Los demás quedan trazados en gaps.md sin caso propio."""
    case_sources: list[str] = Field(default_factory=lambda: list(CASE_SOURCES))

    @model_validator(mode="after")
    def _known(self) -> "DesignCfg":
        unknown = set(self.case_sources) - set(CASE_SOURCES)
        if unknown or not self.case_sources:
            raise ValueError(f"design.case_sources inválido: {sorted(unknown) or 'vacío'}; opciones {CASE_SOURCES}")
        return self


class OutputCfg(_Cfg):
    root_pattern: str = "<epic-folder>/regression-plan"
    gherkin_language: str = "es"
    overwrite: bool = False


class PublicationCfg(_Cfg):
    enabled: bool = False
    test_plan_issue_type: str = "Tarea"
    test_case_issue_type: str = "Test"
    approval_command: str = "Publicalo"
    epic_association: str = Field(default="parent", pattern=r"^(parent|link)$")
    epic_link_type: str = "Relates"
    test_plan_association: str = Field(default="link", pattern=r"^(parent|link)$")
    test_plan_link_type: str = "Relates"
    story_link_type: str = "Relates"
    inherit_epic_components: bool = True
    labels: list[str] = Field(default_factory=lambda: ["qa-regresion"])
    # Etiquetas propias (qa-epic-<KEY>, suite-*, automatización). Sin ellas, lo publicado se encuentra por las
    # relaciones: Tarea hija de la épica y Tests subtareas de la Tarea (como el Test Plan de referencia).
    technical_labels: bool = True
    # Campos de la épica que se copian a la Tarea y a cada Test (por nombre o id, p. ej. "Area Solicitante").
    inherit_epic_fields: list[str] = Field(default_factory=list)
    test_summary_pattern: str = "{tc_id} | {title}"
    project_key: str | None = None

    @model_validator(mode="after")
    def _findable(self) -> "PublicationCfg":
        if not self.technical_labels and (self.epic_association != "parent" or self.test_plan_association != "parent"):
            raise ValueError("sin technical_labels, epic_association y test_plan_association deben ser 'parent' "
                             "para poder encontrar lo ya publicado (idempotencia)")
        return self


class ProjectProfile(_Cfg):
    project: ProjectCfg = Field(default_factory=ProjectCfg)
    atlassian: AtlassianCfg = Field(default_factory=AtlassianCfg)
    documents: DocumentsCfg = Field(default_factory=DocumentsCfg)
    figma: FigmaCfg = Field(default_factory=FigmaCfg)
    design: DesignCfg = Field(default_factory=DesignCfg)
    output: OutputCfg = Field(default_factory=OutputCfg)
    publication: PublicationCfg = Field(default_factory=PublicationCfg)

    # ----------------------------------------------------------------- helpers

    def issue_kind(self, issue_type: str | None) -> str | None:
        """Mapea un tipo de incidencia real a su rol lógico (epic, story, improvement, ...)."""
        if not issue_type:
            return None
        target = normalize(issue_type)
        for kind, aliases in self.atlassian.issue_type_aliases.model_dump().items():
            if any(normalize(a) == target for a in aliases):
                return kind
        return None

    def aliases(self, kind: str) -> list[str]:
        return list(getattr(self.atlassian.issue_type_aliases, kind))


def load_profile(path: str | os.PathLike[str] | None) -> ProjectProfile:
    source = Path(path) if path else EXAMPLE_PROFILE
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    return ProjectProfile.model_validate(data)


_PLACEHOLDER = re.compile(r"<([a-z\-]+)>")


def expand_pattern(pattern: str, values: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"marcador desconocido en el perfil: <{key}>")
        return values[key]

    return _PLACEHOLDER.sub(repl, pattern)


def load_mcp_server_config(cfg: McpCfg) -> dict[str, Any]:
    """Devuelve {command, args, env} del servidor MCP.

    Si `config_file` apunta a un JSON con `mcpServers` (formato Claude), se toma de ahí.
    Las variables de entorno nunca se registran en logs ni artefactos.
    """
    command, args, env = cfg.command, list(cfg.args), dict(cfg.env)
    if cfg.config_file:
        data = json.loads(Path(os.path.expandvars(cfg.config_file)).read_text(encoding="utf-8"))
        server = (data.get("mcpServers") or {}).get(cfg.server_name)
        if not server:
            raise KeyError(f"el servidor '{cfg.server_name}' no está en {cfg.config_file}")
        command = command or server.get("command")
        args = args or list(server.get("args") or [])
        env = {**(server.get("env") or {}), **env}
    if not command:
        raise ValueError("no hay comando configurado para el MCP de Atlassian")
    return {"command": command, "args": args, "env": env}
