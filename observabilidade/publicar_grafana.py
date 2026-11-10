"""Publica o painel e os alertas do Sistema Cripto num Grafana, pela API.

    GRAFANA_SENHA=admin uv run python observabilidade/publicar_grafana.py \\
        --url http://localhost:8080/grafana

O Crypto e um sistema a parte: usa a observabilidade (Grafana, Loki, Tempo,
Prometheus) como um servico, sem nada dele no codigo de quem a mantem. Por isso
o painel e os alertas moram aqui (`grafana/`) e entram no Grafana por esta
chamada -- idempotente: rodar de novo sobrescreve com o que esta no repositorio.

- Pasta `Sistema Cripto` (uid `sistema-cripto`), criada se faltar.
- Painel `grafana/painel-sistema-cripto.json` (uid `crypto-visao`).
- Grupo de alertas `grafana/alertas.json`. Regras publicadas pela API ficam
  travadas na tela (proveniencia `api`): mudou, mude aqui e publique de novo.

As fontes de dados sao procuradas pelos uids `prometheus` e `loki`.
Autenticacao: GRAFANA_TOKEN (conta de servico) ou GRAFANA_USUARIO/GRAFANA_SENHA.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

PASTA = Path(__file__).resolve().parent / "grafana"
PASTA_UID = "sistema-cripto"
PASTA_TITULO = "Sistema Cripto"


def _cliente(url: str) -> httpx.Client:
    token = os.environ.get("GRAFANA_TOKEN")
    if token:
        return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    senha = os.environ.get("GRAFANA_SENHA")
    if not senha:
        sys.exit("Defina GRAFANA_TOKEN ou GRAFANA_SENHA (e GRAFANA_USUARIO, padrao admin).")
    usuario = os.environ.get("GRAFANA_USUARIO", "admin")
    return httpx.Client(base_url=url, auth=(usuario, senha), timeout=20)


def _ok(resposta: httpx.Response, o_que: str) -> dict:
    if resposta.is_error:
        sys.exit(f"{o_que}: HTTP {resposta.status_code} {resposta.text[:400]}")
    return resposta.json() if resposta.content else {}


def _segundos(intervalo: str) -> int:
    unidades = {"s": 1, "m": 60, "h": 3600}
    return int(intervalo[:-1]) * unidades[intervalo[-1]]


def publicar(url: str) -> None:
    with _cliente(url.rstrip("/")) as g:
        for uid in ("prometheus", "loki"):
            _ok(g.get(f"/api/datasources/uid/{uid}"), f"fonte de dados '{uid}'")

        if g.get(f"/api/folders/{PASTA_UID}").status_code == 404:
            _ok(g.post("/api/folders", json={"uid": PASTA_UID, "title": PASTA_TITULO}), "pasta")
        print(f"  pasta     {PASTA_TITULO}")

        painel = json.loads((PASTA / "painel-sistema-cripto.json").read_text(encoding="utf-8"))
        painel.pop("id", None)
        r = _ok(
            g.post(
                "/api/dashboards/db",
                json={"dashboard": painel, "folderUid": PASTA_UID, "overwrite": True},
            ),
            "painel",
        )
        print(f"  painel    {painel['title']} ({r.get('url', '')})")

        alertas = json.loads((PASTA / "alertas.json").read_text(encoding="utf-8"))
        regras = [
            {**regra, "folderUID": PASTA_UID, "ruleGroup": alertas["grupo"], "orgID": 1}
            for regra in alertas["regras"]
        ]
        _ok(
            g.put(
                f"/api/v1/provisioning/folder/{PASTA_UID}/rule-groups/{alertas['grupo']}",
                json={
                    "title": alertas["grupo"],
                    "folderUid": PASTA_UID,
                    "interval": _segundos(alertas["intervalo"]),
                    "rules": regras,
                },
            ),
            "alertas",
        )
        print(f"  alertas   {len(regras)} regras no grupo '{alertas['grupo']}'")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", required=True, help="raiz do Grafana, ex. http://localhost:8080/grafana")
    publicar(p.parse_args().url)


if __name__ == "__main__":
    main()
