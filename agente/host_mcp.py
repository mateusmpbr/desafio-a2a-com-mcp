"""O agente como host MCP: um cliente do SDK oficial falando Streamable HTTP.

O cliente fica vivo entre chamadas (isso e estado de processo, nao de protocolo):
cada request carrega no proprio `_meta` a versao, as capabilities e o
`traceparent`, e o transporte do SDK espelha `MCP-Protocol-Version`,
`Mcp-Method` e `Mcp-Name` nos headers.
"""

from __future__ import annotations

import secrets
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client
from mcp.client.session import ClientSession
from mcp_types import (
    CallToolResult,
    ClientCapabilities,
    ElicitationCapability,
    FormElicitationCapability,
    Implementation,
    InputRequiredResult,
    InputResponses,
    TextResourceContents,
)

PROTOCOLO = "2026-07-28"
CLIENTE = Implementation(name="agente-central-de-salas", version="1.0.0")


class _SessaoSoFormulario(ClientSession):
    """Declara exatamente `{"elicitation": {"form": {}}}`.

    O `ClientSession` do SDK so anuncia elicitation quando recebe um callback, e
    nesse caso anuncia form e url. O agente nao registra callback de proposito (o
    `input_required` tem de chegar cru ate a ponte, nunca ser respondido sozinho)
    e so sabe traduzir form mode, entao anuncia apenas isso.
    """

    def _build_capabilities(self, version: str) -> ClientCapabilities:
        return ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))


class _Cliente(Client):
    async def _build_session(self, exit_stack: AsyncExitStack) -> ClientSession:
        dispatcher = await self._connect(exit_stack, self.mode, self.raise_exceptions)
        return _SessaoSoFormulario(
            dispatcher=dispatcher,
            read_timeout_seconds=self.read_timeout_seconds,
            client_info=self.client_info,
        )


class TraceContext:
    """W3C trace context: o trace-id e da Task inteira, o span-id nasce por request MCP."""

    def __init__(self, trace_id: str, flags: str = "01") -> None:
        self.trace_id = trace_id
        self.flags = flags

    @classmethod
    def do_header(cls, valor: str | None) -> TraceContext | None:
        if not valor:
            return None
        partes = valor.strip().lower().split("-")
        if len(partes) != 4 or len(partes[1]) != 32 or len(partes[2]) != 16 or len(partes[3]) != 2:
            return None
        try:
            int(partes[1], 16), int(partes[2], 16), int(partes[3], 16)
        except ValueError:
            return None
        if partes[1] == "0" * 32:
            return None
        return cls(partes[1], partes[3])

    @classmethod
    def novo(cls) -> TraceContext:
        return cls(secrets.token_hex(16))

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"


class HostMCP:
    def __init__(self, url: str) -> None:
        self.url = url
        self._pilha = AsyncExitStack()
        self._cliente: Client | None = None

    async def abrir(self) -> None:
        # Versao fixada: sem sondagem nem handshake, cada request e autocontido.
        self._cliente = await self._pilha.enter_async_context(
            _Cliente(self.url, mode=PROTOCOLO, client_info=CLIENTE, cache=None)
        )

    async def fechar(self) -> None:
        await self._pilha.aclose()

    @property
    def _conectado(self) -> Client:
        assert self._cliente is not None, "HostMCP.abrir() nao foi chamado"
        return self._cliente

    @staticmethod
    def _meta(trace: TraceContext) -> dict[str, Any]:
        return {"traceparent": trace.traceparent()}

    async def descobrir_tools(self, trace: TraceContext) -> dict[str, Any]:
        resultado = await self._conectado.list_tools(meta=self._meta(trace))
        return {t.name: t for t in resultado.tools}

    async def ler_recurso(self, uri: str, trace: TraceContext) -> str:
        resultado = await self._conectado.read_resource(uri, meta=self._meta(trace))
        return "".join(c.text for c in resultado.contents if isinstance(c, TextResourceContents))

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace: TraceContext,
        *,
        input_responses: InputResponses | None = None,
        request_state: str | None = None,
    ) -> CallToolResult | InputRequiredResult:
        """Um `tools/call` cru: `input_required` volta para quem chamou, sem driver automatico.

        Cada chamada e um request JSON-RPC novo, com id novo cunhado pelo SDK.
        """
        return await self._conectado.session.call_tool(
            nome,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta=self._meta(trace),
            allow_input_required=True,
        )
