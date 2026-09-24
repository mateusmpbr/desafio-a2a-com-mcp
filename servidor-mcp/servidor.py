"""Servidor MCP da Central de Salas (Streamable HTTP, revisao 2026-07-28).

Tres tools (`listar_salas`, `consultar_disponibilidade`, `reservar_sala`), um
resource (`politica://uso`) e o ciclo de MRTR na reserva: quando a sala pedida
esta ocupada, o resolver `escolha_de_sala` devolve um `Elicit` e o SDK responde
`input_required` com o `requestState` selado pelo `RequestStateBoundary`.

    REQUEST_STATE_SECRET=... python servidor-mcp/servidor.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.request_state import RequestStateSecurity
from pydantic import BaseModel, Field, create_model

from registro import RegistroDeRequests

DADOS = Path(__file__).resolve().parent.parent / "dados"
FUSO = timezone(timedelta(hours=-3))
ABERTURA, FECHAMENTO = 8, 20
DURACAO_MAXIMA = timedelta(hours=2)
# Entre 5 e 30 minutos, como pede o enunciado.
VALIDADE_DO_ESTADO = 10 * 60

ERRO_SALA = "Sala inexistente: {}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_DATA = "Data invalida: use ISO 8601 com fuso, por exemplo 2026-11-03T14:00:00-03:00"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


# --- dominio ---------------------------------------------------------------

SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID = {s["id"]: s for s in SALAS}
# Persistencia em memoria: some no restart, como o enunciado permite.
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
POLITICA = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
VERSAO_POLITICA = POLITICA.splitlines()[0].split(":", 1)[1].strip()


def _instante(valor: str) -> datetime:
    try:
        instante = datetime.fromisoformat(valor)
    except ValueError:
        raise ToolError(ERRO_DATA) from None
    if instante.tzinfo is None:
        raise ToolError(ERRO_DATA)
    return instante.astimezone(FUSO)


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as regras da politica; qualquer violacao vira erro de execucao da tool."""
    if sala not in SALAS_POR_ID:
        raise ToolError(ERRO_SALA.format(sala))
    ini, fi = _instante(inicio), _instante(fim)
    if fi <= ini:
        raise ToolError(ERRO_INTERVALO)
    abre = ini.replace(hour=ABERTURA, minute=0, second=0, microsecond=0)
    fecha = ini.replace(hour=FECHAMENTO, minute=0, second=0, microsecond=0)
    if ini < abre or fi > fecha:
        raise ToolError(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise ToolError(ERRO_DURACAO)
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict[str, Any]]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala and _instante(r["inicio"]) < fi and ini < _instante(r["fim"])
    ]


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade >= a pedida: ate tres, por capacidade e id."""
    minima = SALAS_POR_ID[sala]["capacidade"]
    candidatas = [
        s for s in SALAS if s["id"] != sala and s["capacidade"] >= minima and not conflitos(s["id"], ini, fi)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:3]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> dict[str, Any]:
    reserva = {
        "id": f"res-{len(RESERVAS) + 1:04d}",
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    RESERVAS.append(reserva)
    return reserva


# --- contratos de saida ----------------------------------------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# --- servidor --------------------------------------------------------------


def _segredo() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere um com:\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    return segredo


mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    # AES-256-GCM com chave derivada (HKDF) do segredo do ambiente: o estado
    # sobrevive a restart porque a chave nao nasce no processo.
    request_state_security=RequestStateSecurity(
        keys=[_segredo()],
        ttl=VALIDADE_DO_ESTADO,
        bind_principal=None,
    ),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = validar(sala, inicio, fim)
    em_conflito = [ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in conflitos(sala, ini, fi)]
    return Disponibilidade(sala=sala, livre=not em_conflito, conflitos=em_conflito)


def _schema_de_escolha(opcoes: list[str]) -> type[BaseModel]:
    """Schema plano da elicitation: `sala` restrita as alternativas, na ordem da regra."""
    return create_model(
        "EscolhaDeSala",
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[Any] | None:
    """Resolver do MRTR. Roda a cada rodada, antes do corpo da tool.

    Sem conflito, nao pergunta nada (devolve None). Com conflito e alternativas,
    devolve um `Elicit`: na revisao 2026-07-28 o SDK nao abre canal de volta, ele
    termina a resposta com `input_required` e sela o progresso no `requestState`.
    No retry, o resolver recalcula a mesma pergunta e o SDK injeta a resposta.
    """
    ini, fi = validar(sala, inicio, fim)
    if not conflitos(sala, ini, fi):
        return None
    opcoes = alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", _schema_de_escolha(opcoes))


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if not isinstance(escolha, AcceptedElicitation):
        # decline ou cancel: conclui normalmente, sem reservar.
        return ReservaOut(reservado=False, motivo="recusado")
    destino = sala if escolha.data is None else escolha.data.sala
    reserva = criar_reserva(destino, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=destino,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=VERSAO_POLITICA,
    )


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    return POLITICA


def main() -> None:
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = RegistroDeRequests(mcp.streamable_http_app(json_response=True, stateless_http=True))
    print(f"servidor MCP em http://localhost:{porta}/mcp", file=sys.stderr)
    uvicorn.run(app, host=os.environ.get("MCP_HOST", "0.0.0.0"), port=porta, log_level="warning")


if __name__ == "__main__":
    main()
