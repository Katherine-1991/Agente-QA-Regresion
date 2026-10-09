"""Gateway Atlassian simulado a partir de fixtures JSON.

Implementa las mismas herramientas que el MCP local (`jira_search`, `jira_get_issue`,
`confluence_search`, `confluence_get_page`) con un intérprete mínimo de JQL/CQL, de modo que
el adaptador real (paginación, reintentos, deduplicación) se ejercita igual que contra Jira.
Las herramientas de escritura existen solo para detectar intentos: cuentan y fallan.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from qa_regresion.adapters.atlassian_mcp import GatewayError, RateLimitError
from qa_regresion.domain.text_analysis import normalize

READ_TOOLS = {"jira_search", "jira_get_issue", "jira_search_fields", "confluence_search", "confluence_get_page"}
WRITE_TOOLS = {"jira_create_issue", "jira_update_issue", "jira_create_issue_link", "jira_add_comment",
               "jira_delete_issue", "jira_transition_issue"}


class FixtureGateway:
    def __init__(self, jira: dict[str, Any], confluence: dict[str, Any] | None = None, *,
                 server_page_size: int | None = None, fail_first: int = 0, tools: set[str] | None = None):
        self.issues: dict[str, dict[str, Any]] = {i["key"]: i for i in jira.get("issues", [])}
        self.fields: list[dict[str, Any]] = list(jira.get("fields", []))
        self.pages: dict[str, dict[str, Any]] = {str(p["id"]): p for p in (confluence or {}).get("pages", [])}
        self.server_page_size = server_page_size
        self.fail_first = fail_first
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.write_calls: list[str] = []
        self._tools = tools if tools is not None else READ_TOOLS | WRITE_TOOLS
        if confluence is None:
            self._tools = self._tools - {"confluence_search", "confluence_get_page"}

    @classmethod
    def from_dir(cls, path: str | Path, **kwargs: Any) -> "FixtureGateway":
        base = Path(path)
        jira = json.loads((base / "jira.json").read_text(encoding="utf-8"))
        conf_path = base / "confluence.json"
        confluence = json.loads(conf_path.read_text(encoding="utf-8")) if conf_path.exists() else None
        return cls(jira, confluence, **kwargs)

    def available_tools(self) -> set[str]:
        return set(self._tools)

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, dict(arguments)))
        if tool in WRITE_TOOLS:
            self.write_calls.append(tool)
            raise GatewayError(f"escritura no soportada por el fixture: {tool}", status=403)
        if tool not in self._tools:
            raise GatewayError(f"herramienta desconocida: {tool}", status=404)
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RateLimitError("429 Too Many Requests (simulado)")
        handler = getattr(self, "_" + tool)
        return handler(**arguments)

    # ------------------------------------------------------------------ Jira

    def _jira_get_issue(self, issue_key: str, fields: str = "", comment_limit: int = 10, **_: Any) -> dict:
        issue = self.issues.get(issue_key)
        if issue is None:
            raise GatewayError(f"Issue {issue_key} not found (404)", status=404)
        out = copy.deepcopy(issue)
        if comment_limit == 0:
            out.pop("comments", None)
        elif "comments" in out:
            out["comments"] = out["comments"][:comment_limit]
        return out

    def _jira_search(self, jql: str, fields: str = "", limit: int = 10, start_at: int = 0, **_: Any) -> dict:
        predicate = JqlParser(jql).parse()
        matched = [i for i in self.issues.values() if predicate(i, self)]
        matched.sort(key=lambda i: (i["key"].split("-")[0], int(i["key"].split("-")[1])))
        page = min(limit, self.server_page_size or limit)
        chunk = matched[start_at:start_at + page]
        result = []
        for issue in chunk:
            item = copy.deepcopy(issue)
            if "comment" not in fields.split(","):
                item.pop("comments", None)
            result.append(item)
        return {"total": len(matched), "start_at": start_at, "max_results": page, "issues": result}

    def _jira_search_fields(self, keyword: str = "", limit: int = 10, **_: Any) -> list[dict]:
        return [f for f in self.fields if normalize(keyword) in normalize(f.get("name", ""))][:limit]

    def linked_keys(self, key: str) -> set[str]:
        keys: set[str] = set()
        for issue in self.issues.values():
            for link in issue.get("issuelinks", []):
                sides = {(link.get("outward_issue") or {}).get("key"), (link.get("inward_issue") or {}).get("key")}
                if issue["key"] == key:
                    keys |= {s for s in sides if s}
                elif key in sides:
                    keys.add(issue["key"])
        keys.discard(key)
        return keys

    # ------------------------------------------------------------------ Confluence

    def _confluence_search(self, query: str, limit: int = 10, **_: Any) -> list[dict]:
        terms = re.findall(r'(?:text|title)\s*~\s*"([^"]+)"', query)
        out = []
        for page in self.pages.values():
            haystack = normalize(page.get("title", "") + " " + page.get("body", ""))
            if any(normalize(t) in haystack for t in terms):
                out.append({"id": page["id"], "title": page["title"], "url": page.get("url"),
                            "space": page.get("space")})
        return out[:limit]

    def _confluence_get_page(self, page_id: str, **_: Any) -> dict:
        page = self.pages.get(str(page_id))
        if page is None:
            raise GatewayError(f"page {page_id} not found", status=404)
        return {"metadata": {"id": page["id"], "title": page["title"]}, "content": {"value": page.get("body", "")}}


# ---------------------------------------------------------------------- mini JQL

_TOKEN = re.compile(r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<op>!=|~|=|\(|\)|,)|(?P<word>[^\s()=,~!"]+))')


def _field_value(issue: dict[str, Any], name: str, gateway: FixtureGateway) -> list[str]:
    name = name.lower()
    if name == "key":
        return [issue["key"]]
    if name == "project":
        return [issue["key"].split("-")[0]]
    if name in ("parent", "epic link"):
        parent = issue.get("parent")
        return [parent.get("key") if isinstance(parent, dict) else parent] if parent else []
    if name in ("issuetype", "type"):
        return [(issue.get("issue_type") or {}).get("name", "")]
    if name == "status":
        return [(issue.get("status") or {}).get("name", "")]
    if name == "summary":
        return [issue.get("summary", "")]
    if name == "labels":
        return list(issue.get("labels", []))
    if name == "component":
        return list(issue.get("components", []))
    if name == "text":
        comments = " ".join(c.get("body", "") for c in issue.get("comments", []))
        return [" ".join([issue.get("summary", ""), issue.get("description", ""), comments])]
    raise GatewayError(f"campo JQL no soportado por el fixture: {name}", status=400)


class JqlParser:
    def __init__(self, jql: str):
        jql = re.split(r"\border\s+by\b", jql, flags=re.IGNORECASE)[0]
        self.tokens: list[tuple[str, str]] = []
        pos = 0
        while pos < len(jql):
            m = _TOKEN.match(jql, pos)
            if not m or m.end() == pos:
                if jql[pos:].strip() == "":
                    break
                raise GatewayError(f"JQL inválido cerca de: {jql[pos:pos + 20]}", status=400)
            pos = m.end()
            for kind in ("str", "op", "word"):
                if m.group(kind) is not None:
                    value = m.group(kind)
                    if kind == "str":
                        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                    self.tokens.append((kind, value))
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise GatewayError("JQL incompleto", status=400)
        self.i += 1
        return tok

    def parse(self):
        expr = self._or()
        if self.peek() is not None:
            raise GatewayError(f"JQL inesperado: {self.peek()}", status=400)
        return expr

    def _is_word(self, word: str) -> bool:
        tok = self.peek()
        return tok is not None and tok[0] == "word" and tok[1].lower() == word

    def _or(self):
        left = self._and()
        while self._is_word("or"):
            self.take()
            right = self._and()
            left = (lambda a, b: lambda i, g: a(i, g) or b(i, g))(left, right)
        return left

    def _and(self):
        left = self._not()
        while self._is_word("and"):
            self.take()
            right = self._not()
            left = (lambda a, b: lambda i, g: a(i, g) and b(i, g))(left, right)
        return left

    def _not(self):
        if self._is_word("not"):
            self.take()
            inner = self._not()
            return lambda i, g: not inner(i, g)
        if self.peek() == ("op", "("):
            self.take()
            inner = self._or()
            if self.take() != ("op", ")"):
                raise GatewayError("falta ')'", status=400)
            return inner
        return self._clause()

    def _values(self) -> list[str]:
        if self.peek() == ("op", "("):
            self.take()
            values = []
            while self.peek() != ("op", ")"):
                kind, value = self.take()
                if kind != "op":
                    values.append(value)
            self.take()
            return values
        return [self.take()[1]]

    def _clause(self):
        kind, field = self.take()
        if kind not in ("word", "str"):
            raise GatewayError(f"se esperaba un campo y llegó {field}", status=400)
        if field.lower() == "issue" and self._is_word("in"):
            self.take()
            func = self.take()[1]
            args = self._values()
            if func.lower() != "linkedissues":
                raise GatewayError(f"función JQL no soportada: {func}", status=400)
            target = args[0]
            return lambda i, g: i["key"] in g.linked_keys(target)
        negate = False
        if self._is_word("not"):
            self.take()
            negate = True
        op_tok = self.take()
        op = op_tok[1].lower()
        if op == "in":
            values = {normalize(v) for v in self._values()}
            return lambda i, g: (any(normalize(v) in values for v in _field_value(i, field, g))) != negate
        value = self.take()[1]
        if op == "=":
            return lambda i, g: any(normalize(v) == normalize(value) for v in _field_value(i, field, g))
        if op == "!=":
            return lambda i, g: all(normalize(v) != normalize(value) for v in _field_value(i, field, g))
        if op == "~":
            words = normalize(value).split()
            return lambda i, g: any(all(w in normalize(v) for w in words) for v in _field_value(i, field, g))
        raise GatewayError(f"operador JQL no soportado: {op}", status=400)
