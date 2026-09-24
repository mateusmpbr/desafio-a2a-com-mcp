"""A ponte: onde o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED` do A2A.

O agente nao decide nada de dominio. Ele interpreta o pedido em formato fixo,
chama o servidor MCP e traduz o resultado em estado de Task:

- `CallToolResult` com `isError`      -> TASK_STATE_FAILED, com a mensagem da tool
- `CallToolResult` com a reserva      -> TASK_STATE_COMPLETED, com o artifact `reserva`
- `CallToolResult` com reservado=false -> TASK_STATE_CANCELED
- `InputRequiredResult`               -> TASK_STATE_INPUT_REQUIRED, guardando a pausa
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    TextContent,
)

from host_mcp import HostMCP
from tarefas import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, WORKING, Pausa, Task

TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"

PEDIDO = re.compile(
    r"^\s*reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+?)\s*$"
)
ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>\S+)\s*$")
RECUSAR = "recusar"
FORMATO = "Pedido nao reconhecido. Use: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"


def _log(task: Task, texto: str) -> None:
    print(f"[agente] task={task.id} trace={task.trace.trace_id} {texto}", file=sys.stderr, flush=True)


def _texto(resultado: CallToolResult) -> str:
    return " ".join(c.text for c in resultado.content if isinstance(c, TextContent)).strip()


def _versao_da_politica(texto: str) -> str | None:
    primeira = texto.splitlines()[0] if texto else ""
    chave, _, valor = primeira.partition(":")
    return valor.strip() if chave.strip() == "versao" and valor.strip() else None


class Ponte:
    def __init__(self, host: HostMCP) -> None:
        self.host = host

    async def iniciar(self, task: Task, texto: str) -> None:
        """Primeira mensagem da Task: descobre, le a politica e faz o `tools/call` original."""
        task.transitar(WORKING)
        pedido = PEDIDO.match(texto)
        if pedido is None:
            task.transitar(FAILED, FORMATO)
            return
        argumentos = pedido.groupdict()
        try:
            # Descoberta em runtime: a tool precisa existir no tools/list desta rodada.
            tools = await self.host.descobrir_tools(task.trace)
            if TOOL_RESERVA not in tools:
                task.transitar(FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
                return
            task.versao_politica = _versao_da_politica(await self.host.ler_recurso(URI_POLITICA, task.trace))
            _log(task, f"tools/call {TOOL_RESERVA} {json.dumps(argumentos)}")
            resultado = await self.host.chamar_tool(TOOL_RESERVA, argumentos, task.trace)
        except MCPError as erro:
            task.transitar(FAILED, f"Erro do servidor MCP ({erro.error.code}): {erro.error.message}")
            return
        self._traduzir(task, TOOL_RESERVA, argumentos, resultado)

    async def continuar(self, task: Task, texto: str) -> None:
        """Resposta do cliente A2A a uma Task pausada: vira o retry MCP com id novo."""
        pausa = task.pausa
        assert pausa is not None and task.estado == INPUT_REQUIRED
        escolha = ESCOLHA.match(texto)
        valor = escolha.group("valor") if escolha else None

        if valor == RECUSAR:
            resposta = ElicitResult(action="decline")
        elif valor in pausa.opcoes:
            resposta = ElicitResult(action="accept", content={pausa.campo: valor})
        else:
            # Fora do enum: a pergunta continua de pe, com a mesma linha de alternativas.
            task.transitar(INPUT_REQUIRED, self._linha_de_alternativas(pausa.opcoes))
            return

        task.transitar(WORKING)
        _log(task, f"retry tools/call {pausa.tool} action={resposta.action}")
        try:
            # A mesma chave que veio em inputRequests e o requestState ecoado sem modificacao.
            resultado = await self.host.chamar_tool(
                pausa.tool,
                pausa.argumentos,
                task.trace,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
            )
        except MCPError as erro:
            task.transitar(FAILED, f"Erro do servidor MCP ({erro.error.code}): {erro.error.message}")
            return
        task.pausa = None
        self._traduzir(task, pausa.tool, pausa.argumentos, resultado)

    def _traduzir(
        self, task: Task, tool: str, argumentos: dict[str, Any], resultado: CallToolResult | InputRequiredResult
    ) -> None:
        if isinstance(resultado, InputRequiredResult):
            self._pausar(task, tool, argumentos, resultado)
            return
        if resultado.is_error:
            task.transitar(FAILED, _texto(resultado))
            return
        dados = resultado.structured_content or {}
        if dados.get("reservado") is False:
            task.transitar(CANCELED, f"Reserva nao realizada: {dados.get('motivo') or 'recusada'}.")
            return
        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": task.versao_politica,
        }
        task.anexar_artifact("reserva", json.dumps(reserva, ensure_ascii=False))
        task.transitar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")

    def _pausar(self, task: Task, tool: str, argumentos: dict[str, Any], resultado: InputRequiredResult) -> None:
        """O ponto da ponte: o `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED`."""
        pedidos = resultado.input_requests or {}
        if len(pedidos) != 1 or not resultado.request_state:
            task.transitar(FAILED, "O servidor MCP pediu uma entrada que o agente nao sabe traduzir")
            return
        chave, pedido = next(iter(pedidos.items()))
        formulario = isinstance(pedido, ElicitRequest) and isinstance(pedido.params, ElicitRequestFormParams)
        propriedades = (pedido.params.requested_schema.get("properties") or {}) if formulario else {}
        if len(propriedades) != 1:
            task.transitar(FAILED, "O servidor MCP pediu uma entrada que o agente nao sabe traduzir")
            return
        campo, definicao = next(iter(propriedades.items()))
        opcoes = list(definicao.get("enum") or ([definicao["const"]] if "const" in definicao else []))
        if not opcoes:
            task.transitar(FAILED, "O servidor MCP pediu uma entrada que o agente nao sabe traduzir")
            return
        task.pausa = Pausa(
            tool=tool,
            argumentos=argumentos,
            chave=chave,
            campo=campo,
            opcoes=opcoes,
            request_state=resultado.request_state,
        )
        _log(task, f"input_required chave={chave} opcoes={opcoes}")
        task.transitar(INPUT_REQUIRED, self._linha_de_alternativas(opcoes))

    @staticmethod
    def _linha_de_alternativas(opcoes: list[str]) -> str:
        return "alternativas: " + ", ".join(opcoes)
