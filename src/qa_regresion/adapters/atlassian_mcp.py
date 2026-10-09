"""Adaptador de lectura de Jira y Confluence a través del MCP local de Atlassian.

Capas:
- `AtlassianGateway`: llamada cruda a una herramienta MCP (stdio real, fixtures o fakes de test).
- `ReadOnlyGuard`: lista blanca de herramientas de lectura; cualquier otra lanza WriteAttemptBlocked.
- `AtlassianReader`: búsqueda de épicas, relaciones, paginación, reintentos acotados, campos
  configurables, Confluence y deduplicación por clave.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from qa_regresion.config import ProjectProfile
from qa_regresion.domain.enums import ReadStatus, SourceType
from qa_regresion.domain.models import EpicCandidate, ISSUE_KEY, SourceRecord
from qa_regresion.domain.text_analysis import normalize
from qa_regresion.domain.validation import WriteAttemptBlocked, detect_untrusted_instructions

log = logging.getLogger(__name__)

# Verbo de la acción (después del prefijo jira_/confluence_) que delata una escritura.
_WRITE_VERB = re.compile(r"^(create|update|delete|remove|add|edit|transition|assign|upload|batch|move|link|"
                         r"set|publish|write|attach)", re.IGNORECASE)


def looks_like_write(tool: str) -> bool:
    action = tool.split("_", 1)[1] if "_" in tool else tool
    return bool(_WRITE_VERB.match(action))


class GatewayError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class RateLimitError(GatewayError):
    def __init__(self, message: str = "rate limit"):
        super().__init__(message, status=429, retryable=True)


class AtlassianGateway(Protocol):
    def available_tools(self) -> set[str]: ...

    def call(self, tool: str, arguments: dict[str, Any]) -> Any: ...


@dataclass
class CallRecord:
    tool: str
    arguments: dict[str, Any]
    ok: bool = True
    error: str | None = None


class ReadOnlyGuard:
    """Envuelve un gateway y solo deja pasar herramientas de lectura de la lista blanca."""

    def __init__(self, inner: AtlassianGateway, allowed_tools: list[str]):
        self._inner = inner
        # Aunque el perfil liste por error una herramienta de escritura, nunca se habilita.
        self._allowed = {t for t in allowed_tools if not looks_like_write(t)}
        self.calls: list[CallRecord] = []
        self.blocked: list[str] = []

    def available_tools(self) -> set[str]:
        return {t for t in self._inner.available_tools() if t in self._allowed}

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if tool not in self._allowed:
            self.blocked.append(tool)
            raise WriteAttemptBlocked(f"operación remota no permitida en modo lectura: {tool}")
        record = CallRecord(tool=tool, arguments=_redact(arguments))
        self.calls.append(record)
        try:
            return self._inner.call(tool, arguments)
        except Exception as exc:  # noqa: BLE001 - se registra y se propaga
            record.ok = False
            record.error = str(exc)[:300]
            raise

    @property
    def write_calls(self) -> int:
        return len(self.blocked)


def _redact(arguments: dict[str, Any]) -> dict[str, Any]:
    redacted = {}
    for key, value in arguments.items():
        if re.search(r"token|password|secret|auth", key, re.IGNORECASE):
            redacted[key] = "***"
        else:
            redacted[key] = value if not isinstance(value, str) else value[:300]
    return redacted


# --------------------------------------------------------------------------- MCP stdio real


class McpStdioGateway:
    """Cliente MCP por stdio con fachada sincrónica (loop asyncio en un hilo propio).

    Las variables de entorno del servidor (que pueden incluir rutas a credenciales) se pasan al
    proceso hijo y nunca se escriben en logs.
    """

    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None,
                 timeout_seconds: float = 90, cwd: str | None = None):
        self._cwd = cwd  # el MCP solo sube adjuntos que estén dentro de su carpeta de trabajo
        self._command = command
        self._args = args
        self._env = env or {}
        self._timeout = timeout_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._tools: set[str] = set()

    def start(self) -> "McpStdioGateway":
        try:
            from mcp import ClientSession, StdioServerParameters  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise GatewayError("el paquete 'mcp' no está instalado (pip install mcp)") from exc
        self._thread = threading.Thread(target=self._run_loop, name="mcp-atlassian", daemon=True)
        self._thread.start()
        if not self._ready.wait(self._timeout):
            raise GatewayError("timeout al iniciar el servidor MCP de Atlassian")
        if self._error:
            raise GatewayError(f"no se pudo iniciar el MCP de Atlassian: {self._error}") from self._error
        return self

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            self._ready.set()
        finally:
            loop.close()

    async def _serve(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=self._command, args=self._args, env=self._env or None,
                                        cwd=self._cwd)
        self._stop = asyncio.Event()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                self._tools = {t.name for t in tools.tools}
                self._session = session
                self._ready.set()
                await self._stop.wait()

    def available_tools(self) -> set[str]:
        return set(self._tools)

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if self._session is None or self._loop is None:
            raise GatewayError("el gateway MCP no está iniciado")
        future = asyncio.run_coroutine_threadsafe(self._session.call_tool(tool, arguments), self._loop)
        try:
            result = future.result(self._timeout)
        except TimeoutError as exc:
            raise GatewayError(f"timeout en {tool}", retryable=True) from exc
        text = "\n".join(getattr(c, "text", "") for c in (result.content or []) if getattr(c, "text", None))
        if getattr(result, "isError", False):
            lowered = text.lower()
            status = 429 if ("429" in lowered or "rate limit" in lowered) else (
                403 if "403" in lowered else 404 if "404" in lowered else None)
            if status == 429:
                raise RateLimitError(text[:300])
            raise GatewayError(text[:500], status=status, retryable=status in (None, 500, 502, 503))
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    def close(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=15)


# --------------------------------------------------------------------------- lector de alto nivel


@dataclass
class AtlassianCollection:
    epic: SourceRecord
    issues: list[SourceRecord] = field(default_factory=list)
    comments: list[SourceRecord] = field(default_factory=list)
    confluence_pages: list[SourceRecord] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    reference_mapping: dict[str, Any] | None = None
    epic_fields: dict[str, Any] = field(default_factory=dict)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("name") or value.get("value") or value.get("display_name") or value.get("key")
    return str(value)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "value" in value and len(value) <= 3:
            return _as_text(value["value"])
        if value.get("type") == "doc":  # ADF
            return _adf_to_text(value)
        return "\n".join(_as_text(v) for v in value.values() if isinstance(v, (str, dict, list)))
    if isinstance(value, list):
        return "\n".join(_as_text(v) for v in value)
    return str(value)


def _adf_to_text(node: Any) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [_adf_to_text(c) for c in node.get("content", [])]
        sep = "\n" if node.get("type") in ("paragraph", "listItem", "heading", "doc", "bulletList",
                                           "orderedList") else ""
        return sep.join(p for p in parts if p)
    if isinstance(node, list):
        return "\n".join(_adf_to_text(n) for n in node)
    return ""


def simplify_issue(raw: dict[str, Any]) -> dict[str, Any]:
    """Acepta el formato simplificado de mcp-atlassian o el formato REST (`fields`)."""
    if "fields" not in raw:
        return raw
    f = raw.get("fields") or {}
    out = {"key": raw.get("key"), "id": raw.get("id"), "summary": f.get("summary"),
           "description": _as_text(f.get("description")), "status": f.get("status"),
           "issue_type": f.get("issuetype"), "labels": f.get("labels") or [],
           "components": [_name(c) for c in f.get("components") or []], "parent": f.get("parent"),
           "resolution": f.get("resolution"), "priority": f.get("priority"), "project": f.get("project"),
           "issuelinks": f.get("issuelinks") or []}
    comments = (f.get("comment") or {}).get("comments") if isinstance(f.get("comment"), dict) else None
    if comments:
        out["comments"] = [{"id": c.get("id"), "body": _as_text(c.get("body")),
                            "author": _name(c.get("author")), "created": c.get("created")} for c in comments]
    for key, value in f.items():
        if key.startswith("customfield_") and value is not None:
            out[key] = {"value": value}
    return out


class AtlassianReader:
    def __init__(self, gateway: AtlassianGateway, profile: ProjectProfile,
                 sleep: Callable[[float], None] = time.sleep, base_url: str | None = None):
        self.gateway = gateway
        self.profile = profile
        self.cfg = profile.atlassian
        self.tools = profile.atlassian.mcp.tools
        self._sleep = sleep
        self._base_url = base_url
        self.limitations: list[str] = []
        self._available = gateway.available_tools()

    # ------------------------------------------------------------- llamadas con reintento

    def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        attempts = self.cfg.retry.max_attempts
        delay = self.cfg.retry.backoff_seconds
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self.gateway.call(tool, arguments)
            except WriteAttemptBlocked:
                raise
            except GatewayError as exc:
                last = exc
                if not exc.retryable or attempt == attempts:
                    break
                log.info("reintento %s/%s de %s: %s", attempt, attempts, tool, exc)
                self._sleep(delay * (2 ** (attempt - 1)))
        assert last is not None
        raise last

    def has_tool(self, logical: str) -> bool:
        return getattr(self.tools, logical) in self._available

    # ------------------------------------------------------------- búsqueda paginada

    def search_all(self, jql: str, fields: str) -> tuple[list[dict[str, Any]], bool]:
        """Devuelve (issues, completo). Pagina por start_at o next_page_token, con tope de páginas."""
        issues: dict[str, dict[str, Any]] = {}
        start_at, token = 0, None
        for _page in range(self.cfg.max_pages):
            args: dict[str, Any] = {"jql": jql, "fields": fields, "limit": self.cfg.page_size, "start_at": start_at}
            if self.cfg.jira_projects:
                args["projects_filter"] = ",".join(self.cfg.jira_projects)
            if token:
                args["page_token"] = token
            data = self._call(self.tools.jira_search, args) or {}
            batch = [simplify_issue(i) for i in data.get("issues", [])]
            for issue in batch:
                if issue.get("key"):
                    issues.setdefault(issue["key"], issue)
            token = data.get("next_page_token")
            total = data.get("total")
            start_at += len(batch)
            if not batch:
                return list(issues.values()), True
            if token:
                continue
            if isinstance(total, int) and total >= 0 and start_at >= total:
                return list(issues.values()), True
            if len(batch) < self.cfg.page_size and not (isinstance(total, int) and total > start_at):
                return list(issues.values()), True
        self.limitations.append(f"Búsqueda truncada tras {self.cfg.max_pages} páginas: {jql}")
        return list(issues.values()), False

    def _fields(self, extra: tuple[str, ...] = ()) -> str:
        base = ["summary", "description", "status", "issuetype", "priority", "labels", "components", "parent",
                "issuelinks", "resolution", "project", "created", "updated", *extra]
        return ",".join(dict.fromkeys([*base, *self.cfg.acceptance_criteria_fields]))

    def get_issue(self, key: str, *, with_comments: bool = True, all_fields: bool = False) -> dict[str, Any]:
        args: dict[str, Any] = {"issue_key": key, "fields": "*all" if all_fields else self._fields(("comment",)),
                                "comment_limit": self.cfg.comment_limit if with_comments else 0,
                                "update_history": False}
        data = self._call(self.tools.jira_get_issue, args) or {}
        if isinstance(data, dict) and "issue" in data and isinstance(data["issue"], dict):
            data = data["issue"]
        return simplify_issue(data)

    # ------------------------------------------------------------- épica

    @staticmethod
    def _jql_string(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def resolve_epic(self, query: str) -> list[EpicCandidate]:
        query = query.strip()
        epic_types = ", ".join(self._jql_string(t) for t in self.profile.aliases("epic"))
        if re.match(ISSUE_KEY, query.upper()):
            try:
                issue = self.get_issue(query.upper(), with_comments=False)
            except GatewayError as exc:
                if exc.status == 404:
                    return []
                raise
            if self.profile.issue_kind(_name(issue.get("issue_type"))) != "epic":
                self.limitations.append(f"{query} existe pero no es de un tipo de épica configurado")
                return []
            return [self._candidate(issue)]
        jql = f"issuetype in ({epic_types}) AND summary ~ {self._jql_string(query)} ORDER BY created DESC"
        issues, _ = self.search_all(jql, "summary,status,issuetype,project")
        wanted = normalize(query).split()
        matches = [i for i in issues if all(w in normalize(i.get("summary") or "") for w in wanted)]
        return [self._candidate(i) for i in matches]

    def _candidate(self, issue: dict[str, Any]) -> EpicCandidate:
        project = issue.get("project")
        project_key = project.get("key") if isinstance(project, dict) else issue.get("key", "").split("-")[0]
        return EpicCandidate(key=issue["key"], summary=issue.get("summary") or issue["key"],
                             status=_name(issue.get("status")), project_key=project_key)

    # ------------------------------------------------------------- recolección completa

    def collect(self, epic_key: str) -> AtlassianCollection:
        epic_raw = self.get_issue(epic_key)
        epic = self._to_record(epic_raw, relation="epic")
        collection = AtlassianCollection(epic=epic)
        collection.epic_fields = self._inherited_fields(epic_key, collection)
        related: dict[str, dict[str, Any]] = {}

        # 1. Hijas de la épica (consultas configurables; una que falle no corta el resto).
        for template in self.cfg.epic_child_jql:
            jql = template.format(epic_key=epic_key)
            try:
                issues, complete = self.search_all(jql, self._fields())
                for issue in issues:
                    related.setdefault(issue["key"], {**issue, "_relation": "child"})
                if not complete:
                    collection.limitations.append(f"Hijas de la épica incompletas ({jql})")
            except GatewayError as exc:
                collection.limitations.append(f"Consulta de hijas no disponible ({jql}): {str(exc)[:120]}")

        # 2. Enlaces de la épica y de sus hijas.
        linked_keys: dict[str, str] = {}
        for issue in [epic_raw, *related.values()]:
            for link in issue.get("issuelinks") or []:
                for side in ("outward_issue", "inward_issue", "outwardIssue", "inwardIssue"):
                    target = link.get(side)
                    if isinstance(target, dict) and target.get("key") and target["key"] != epic_key:
                        link_type = _name(link.get("type")) or "link"
                        linked_keys.setdefault(target["key"], f"linked:{link_type}:{issue.get('key')}")
        pending = [k for k in linked_keys if k not in related]
        for chunk in (pending[i:i + 40] for i in range(0, len(pending), 40)):
            jql = f"key in ({', '.join(chunk)})"
            try:
                issues, _ = self.search_all(jql, self._fields())
                for issue in issues:
                    related.setdefault(issue["key"], {**issue, "_relation": linked_keys[issue["key"]]})
            except GatewayError as exc:
                collection.limitations.append(f"No se pudieron leer incidencias enlazadas: {str(exc)[:120]}")

        # 3. Mejoras, fuera de alcance e incidencias relacionadas por texto.
        if self.cfg.related_text_search:
            kinds = [*self.profile.aliases("improvement"), *self.profile.aliases("out_of_scope"),
                     *self.profile.aliases("incident")]
            types = ", ".join(self._jql_string(t) for t in kinds)
            jql = f"issuetype in ({types}) AND text ~ {self._jql_string(epic_key)}"
            try:
                issues, _ = self.search_all(jql, self._fields())
                for issue in issues:
                    related.setdefault(issue["key"], {**issue, "_relation": "text-search"})
            except GatewayError as exc:
                collection.limitations.append(f"Búsqueda por texto no disponible: {str(exc)[:120]}")

        # 4. Detalle con comentarios de las incidencias funcionales.
        functional_kinds = {"story", "improvement", "out_of_scope", "incident"}
        for key, issue in sorted(related.items()):
            relation = issue.pop("_relation", "related")
            kind = self.profile.issue_kind(_name(issue.get("issue_type")))
            if kind in functional_kinds:
                try:
                    detail = self.get_issue(key)
                    issue = {**issue, **detail}
                    read_status = ReadStatus.COMPLETE
                except GatewayError as exc:
                    read_status = ReadStatus.PARTIAL
                    collection.limitations.append(f"{key}: comentarios no leídos ({str(exc)[:80]})")
            else:
                read_status = ReadStatus.COMPLETE
            collection.issues.append(self._to_record(issue, relation=relation, read_status=read_status))

        collection.comments = self._comments_of(epic_raw, epic.source_id)
        for record in collection.issues:
            raw_comments = record.fields.pop("_comments", [])
            collection.comments.extend(self._comment_records(raw_comments, record.source_id))
        epic.fields.pop("_comments", None)

        # 5. Confluence.
        collection.confluence_pages = self._confluence(epic_key, epic.title, collection)

        # 6. Incidencia de referencia (solo lectura) para descubrir mapeo de campos/enlaces.
        if self.cfg.reference_issue:
            collection.reference_mapping = self.read_reference_issue(self.cfg.reference_issue, collection)

        collection.limitations = [*self.limitations, *collection.limitations]
        return collection

    def _inherited_fields(self, epic_key: str, collection: AtlassianCollection) -> dict[str, Any]:
        """Lee de la épica los campos que la Tarea y los Tests deben copiar (p. ej. "Area Solicitante")."""
        wanted = self.profile.publication.inherit_epic_fields
        if not wanted:
            return {}
        fields: dict[str, dict[str, Any]] = {}
        for name in wanted:
            if name.startswith("customfield_"):
                fields[name] = {"name": name, "schema": {}}
                continue
            try:
                found = self._call("jira_search_fields", {"keyword": name, "limit": 10}) or []
            except GatewayError as exc:
                collection.limitations.append(f"No se pudo resolver el campo «{name}»: {str(exc)[:100]}")
                continue
            match = next((f for f in found if isinstance(f, dict) and normalize(f.get("name", "")) == normalize(name)),
                         None)
            if match is None:
                collection.limitations.append(f"El campo «{name}» no existe en Jira; no se hereda de la épica.")
                continue
            fields[match["id"]] = {"name": match["name"], "schema": match.get("schema") or {}}
        if not fields:
            return {}
        try:
            data = simplify_issue(self._call(self.tools.jira_get_issue, {
                "issue_key": epic_key, "fields": ",".join(fields), "comment_limit": 0,
                "update_history": False}) or {})
        except GatewayError as exc:
            collection.limitations.append(f"No se pudieron leer los campos heredables de {epic_key}: {str(exc)[:100]}")
            return {}
        out: dict[str, Any] = {}
        for field_id, meta in fields.items():
            raw = data.get(field_id)
            value = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
            if value in (None, "", [], {}):
                collection.limitations.append(f"La épica {epic_key} no tiene valor en «{meta['name']}»; la "
                                              f"publicación puede fallar si Jira lo exige.")
                continue
            schema = meta["schema"]
            values = value if isinstance(value, list) else [value]
            texts = [v.get("value") if isinstance(v, dict) else str(v) for v in values]
            if schema.get("type") == "array" and schema.get("items") == "option":
                jira_value: Any = [{"value": t} for t in texts]
            elif schema.get("type") == "option":
                jira_value = {"value": texts[0]}
            else:
                jira_value = value
            out[field_id] = {"name": meta["name"], "value": ", ".join(texts), "jira_value": jira_value}
        return out

    def _comments_of(self, raw: dict[str, Any], key: str) -> list[SourceRecord]:
        return self._comment_records(raw.get("comments") or [], key)

    def _comment_records(self, comments: list[dict[str, Any]], parent: str) -> list[SourceRecord]:
        records = []
        seen: set[str] = set()
        for i, comment in enumerate(comments):
            cid = str(comment.get("id") or i + 1)
            if cid in seen:
                continue
            seen.add(cid)
            body = _as_text(comment.get("body"))
            records.append(SourceRecord(
                source_id=f"{parent}#comment-{cid}", source_type=SourceType.JIRA_COMMENT,
                title=f"Comentario {cid} de {parent}", location=f"{self._browse(parent)}?focusedCommentId={cid}",
                read_status=ReadStatus.COMPLETE, parent_id=parent, text=body, content_hash=_hash(body),
                fields={"author": _name(comment.get("author")), "created": comment.get("created")},
                untrusted_instructions=detect_untrusted_instructions(body)))
        return records

    def _browse(self, key: str) -> str:
        return f"{self._base_url.rstrip('/')}/browse/{key}" if self._base_url else f"jira:{key}"

    def _to_record(self, issue: dict[str, Any], *, relation: str,
                   read_status: ReadStatus = ReadStatus.COMPLETE) -> SourceRecord:
        issue_type = _name(issue.get("issue_type"))
        kind = self.profile.issue_kind(issue_type)
        source_type = {
            "epic": SourceType.JIRA_EPIC, "story": SourceType.JIRA_STORY,
            "improvement": SourceType.JIRA_IMPROVEMENT, "out_of_scope": SourceType.JIRA_OUT_OF_SCOPE,
            "incident": SourceType.JIRA_INCIDENT,
        }.get(kind or "", SourceType.JIRA_OTHER)
        description = _as_text(issue.get("description"))
        fields: dict[str, Any] = {
            "labels": issue.get("labels") or [],
            "components": [c for c in (_name(c) for c in issue.get("components") or []) if c],
            "resolution": _name(issue.get("resolution")),
            "priority": _name(issue.get("priority")),
            "issuelinks": [
                {"type": _name(link.get("type")),
                 "inward": (link.get("type") or {}).get("inward") if isinstance(link.get("type"), dict) else None,
                 "outward": (link.get("type") or {}).get("outward") if isinstance(link.get("type"), dict) else None,
                 "outward_issue": (link.get("outward_issue") or link.get("outwardIssue") or {}).get("key"),
                 "inward_issue": (link.get("inward_issue") or link.get("inwardIssue") or {}).get("key")}
                for link in issue.get("issuelinks") or []],
            "_comments": issue.get("comments") or [],
        }
        ac_texts = []
        for field_id, value in issue.items():
            if not field_id.startswith("customfield_"):
                continue
            label = value.get("name") if isinstance(value, dict) else None
            if field_id in self.cfg.acceptance_criteria_fields or (
                    label and any(normalize(label) == normalize(f) for f in self.cfg.acceptance_criteria_fields)):
                text = _as_text(value.get("value") if isinstance(value, dict) else value)
                if text.strip():
                    ac_texts.append(text)
        if ac_texts:
            fields["acceptance_criteria"] = "\n".join(ac_texts)
        parent = issue.get("parent")
        parent_key = parent.get("key") if isinstance(parent, dict) else parent
        text = "\n".join(t for t in [description, fields.get("acceptance_criteria", "")] if t)
        return SourceRecord(
            source_id=issue["key"], source_type=source_type, title=issue.get("summary") or issue["key"],
            location=self._browse(issue["key"]), read_status=read_status, issue_type=issue_type,
            status=_name(issue.get("status")), relation=relation, parent_id=parent_key,
            content_hash=_hash(text + (issue.get("summary") or "")), text=description, fields=fields,
            untrusted_instructions=detect_untrusted_instructions(text))

    # ------------------------------------------------------------- Confluence

    def _confluence(self, epic_key: str, epic_title: str, collection: AtlassianCollection) -> list[SourceRecord]:
        if not self.has_tool("confluence_search"):
            collection.limitations.append(
                "Confluence no disponible: el MCP no expone herramientas confluence_* (lectura omitida)")
            return []
        cql = f'type = page AND (text ~ {self._jql_string(epic_key)} OR title ~ {self._jql_string(epic_key)})'
        if self.cfg.confluence_spaces:
            cql += " AND space in (" + ", ".join(self._jql_string(s) for s in self.cfg.confluence_spaces) + ")"
        try:
            found = self._call(self.tools.confluence_search, {"query": cql, "limit": 25}) or []
        except GatewayError as exc:
            collection.limitations.append(f"Búsqueda de Confluence fallida: {str(exc)[:120]}")
            return []
        pages = found.get("results", found) if isinstance(found, dict) else found
        records: dict[str, SourceRecord] = {}
        for page in pages or []:
            page_id = str(page.get("id") or "")
            if not page_id or page_id in records:
                continue
            read_status = ReadStatus.COMPLETE
            text = _as_text(page.get("content") or page.get("excerpt") or "")
            if self.has_tool("confluence_get_page"):
                try:
                    detail = self._call(self.tools.confluence_get_page, {"page_id": page_id,
                                                                         "include_metadata": True})
                    content = detail.get("content") if isinstance(detail, dict) else detail
                    text = _as_text(content.get("value") if isinstance(content, dict) else content) or text
                except GatewayError as exc:
                    read_status = ReadStatus.PARTIAL
                    collection.limitations.append(f"Página Confluence {page_id} leída parcialmente: {exc}")
            else:
                read_status = ReadStatus.PARTIAL
            records[page_id] = SourceRecord(
                source_id=f"confluence:{page_id}", source_type=SourceType.CONFLUENCE_PAGE,
                title=page.get("title") or page_id, location=page.get("url") or f"confluence:{page_id}",
                read_status=read_status, relation="mentions-epic", content_hash=_hash(text), text=text,
                untrusted_instructions=detect_untrusted_instructions(text))
        return list(records.values())

    # ------------------------------------------------------------- referencia de mapeo

    def read_reference_issue(self, key: str, collection: AtlassianCollection) -> dict[str, Any] | None:
        """Lee (sin modificar) un Test Plan de referencia y sus Tests para descubrir el modelo de relaciones:
        Test Plan ↔ épica, Test ↔ Test Plan y Test ↔ historias. Compara el resultado con el perfil."""
        fields = "summary,issuetype,parent,components,labels,issuelinks,subtasks"
        try:
            plan = simplify_issue(self._call(self.tools.jira_get_issue, {
                "issue_key": key, "fields": fields, "comment_limit": 0, "update_history": False}) or {})
        except GatewayError as exc:
            collection.limitations.append(f"No se pudo leer la incidencia de referencia {key}: {str(exc)[:120]}")
            return None
        epic_key = collection.epic.source_id
        story_keys = {r.source_id for r in collection.issues if r.source_type == SourceType.JIRA_STORY}

        def kind_of(ref: Any) -> str | None:
            if not isinstance(ref, dict):
                return None
            itype = ((ref.get("fields") or {}).get("issuetype") or {}).get("name") or _name(ref.get("issue_type"))
            return self.profile.issue_kind(itype)

        parent = plan.get("parent") if isinstance(plan.get("parent"), dict) else {"key": plan.get("parent")}
        parent_is_epic = parent.get("key") == epic_key or kind_of(parent) == "epic"
        subtasks = [s for s in plan.get("subtasks") or [] if isinstance(s, dict) and s.get("key")]
        sample_keys = [s["key"] for s in subtasks[:5]]
        tests = []
        for test_key in sample_keys:
            try:
                tests.append(simplify_issue(self._call(self.tools.jira_get_issue, {
                    "issue_key": test_key, "fields": fields, "comment_limit": 0, "update_history": False}) or {}))
            except GatewayError as exc:
                collection.limitations.append(f"No se pudo leer el Test de referencia {test_key}: {str(exc)[:80]}")
        story_link_types: dict[str, int] = {}
        test_plan_links = 0
        for test in tests:
            for link in test.get("issuelinks") or []:
                other = link.get("outward_issue") or link.get("inward_issue") or {}
                name = _name(link.get("type")) or ""
                if other.get("key") == key:
                    test_plan_links += 1
                elif other.get("key") in story_keys or kind_of(other) == "story":
                    story_link_types[name] = story_link_types.get(name, 0) + 1
        tests_parented = bool(tests) and all(
            ((t.get("parent") or {}).get("key") if isinstance(t.get("parent"), dict) else t.get("parent")) == key
            for t in tests)
        test_types = sorted({_name(t.get("issue_type")) or "" for t in tests} - {""})
        recommended = {
            "test_plan_issue_type": _name(plan.get("issue_type")),
            "test_case_issue_type": test_types[0] if test_types else None,
            "epic_association": "parent" if parent_is_epic else "link",
            "test_plan_association": "parent" if tests_parented else ("link" if test_plan_links else None),
            "story_link_type": max(story_link_types, key=story_link_types.get) if story_link_types else None,
        }
        pub = self.profile.publication
        mismatches = [f"{field}: perfil={getattr(pub, field)!r} referencia={value!r}"
                      for field, value in recommended.items() if value and getattr(pub, field) != value]
        for mismatch in mismatches:
            collection.limitations.append(f"El perfil difiere del Test Plan de referencia {key} en {mismatch}")
        return {
            "reference_issue": key,
            "summary": plan.get("summary"),
            "test_plan": {
                "issue_type": _name(plan.get("issue_type")), "parent": parent.get("key"),
                "parent_is_epic": parent_is_epic, "components": [_name(c) for c in plan.get("components") or []],
                "labels": plan.get("labels") or [],
                "links": [{"type": _name(link.get("type")),
                           "issue": (link.get("outward_issue") or link.get("inward_issue") or {}).get("key")}
                          for link in plan.get("issuelinks") or []],
            },
            "tests": {
                "count": len(subtasks), "sample": sample_keys, "issue_types": test_types,
                "parent_is_test_plan": tests_parented, "links_to_test_plan": test_plan_links,
                "story_link_types": story_link_types,
                "components": sorted({_name(c) for t in tests for c in t.get("components") or []} - {None}),
                "labels": sorted({lab for t in tests for lab in t.get("labels") or []}),
            },
            "recommended_profile": recommended,
            "profile_mismatches": mismatches,
            "note": "Leída solo como referencia de relaciones; no se modificó.",
        }
