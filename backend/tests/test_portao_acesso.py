"""Acesso em producao: o portao do Tailscale e a trava de origem (auditoria 09/11/2026).

Dois contornos medidos no codigo anterior, os dois com a API em `0.0.0.0` na rede
`crypto-prd`:

1. O portao comparava so o cabecalho `Tailscale-User-Login`. O `tailscale serve`
   sobrescreve o que vem de FORA, mas o host e qualquer conteiner da rede chegam a
   `http://<ip-da-bridge>:8000` direto, sem passar pelo serve, e mandam o cabecalho
   que quiserem: `POST /api/risk/circuit-breaker/reset` com o login do dono -> 200.
2. A trava de origem aceitava `Origin` de loopback em qualquer porta tambem em
   producao. La a identidade do Tailscale e ambiente (o serve a poe em todo pedido do
   aparelho do dono), entao uma pagina em `http://localhost:XXXX` no aparelho do dono
   acionava `resume-all` com um POST simples (sem preflight).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from crypto_traders.api import app as modulo_app
from crypto_traders.api.app import PortaoTailscale, create_app
from crypto_traders.api.deps import origem_confiavel

LOGIN = "paulo@exemplo.com"
#: IP do conteiner `crypto-prd-tailscale` na bridge (o serve conecta dele).
IP_DO_SERVE = "172.18.0.4"
#: O gateway da bridge: e de onde o HOST chega a um conteiner da rede.
IP_DO_HOST = "172.18.0.1"


def _app(proxies) -> tuple[FastAPI, dict]:
    efeitos = {"rearmes": 0}
    app = FastAPI()

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.post("/api/risk/circuit-breaker/reset")
    async def rearmar():
        efeitos["rearmes"] += 1
        return {"rearmado": True}

    @app.websocket("/ws")
    async def tempo_real(ws: WebSocket):
        await ws.accept()
        await ws.send_text("oi")
        await ws.close()

    app.add_middleware(PortaoTailscale, login=LOGIN, proxies=proxies)
    return app, efeitos


class TestCabecalhoSoValeVindoDoServe:
    def test_cabecalho_forjado_pelo_host_e_recusado_e_a_rota_nao_roda(self):
        """O contorno do relatorio: curl do host com o login do dono."""
        app, efeitos = _app([IP_DO_SERVE])
        do_host = TestClient(app, client=(IP_DO_HOST, 51000))

        resposta = do_host.post(
            "/api/risk/circuit-breaker/reset", headers={"Tailscale-User-Login": LOGIN}
        )

        assert resposta.status_code == 403
        assert efeitos["rearmes"] == 0

    def test_cabecalho_forjado_de_outro_conteiner_e_recusado(self):
        app, efeitos = _app([IP_DO_SERVE])
        do_postgres = TestClient(app, client=("172.18.0.2", 40000))
        assert (
            do_postgres.post(
                "/api/risk/circuit-breaker/reset", headers={"Tailscale-User-Login": LOGIN}
            ).status_code
            == 403
        )
        assert efeitos["rearmes"] == 0

    def test_o_mesmo_pedido_vindo_do_serve_passa(self):
        app, efeitos = _app([IP_DO_SERVE])
        pelo_serve = TestClient(app, client=(IP_DO_SERVE, 40000))
        resposta = pelo_serve.post(
            "/api/risk/circuit-breaker/reset", headers={"Tailscale-User-Login": LOGIN}
        )
        assert resposta.json() == {"rearmado": True}
        assert efeitos["rearmes"] == 1

    def test_websocket_forjado_nao_abre(self):
        app, _ = _app([IP_DO_SERVE])
        do_host = TestClient(app, client=(IP_DO_HOST, 51000))
        with (
            pytest.raises(WebSocketDisconnect),
            do_host.websocket_connect("/ws", headers={"Tailscale-User-Login": LOGIN}),
        ):
            pass

    def test_ipv4_mapeado_em_ipv6_e_o_mesmo_endereco(self):
        app, _ = _app([IP_DO_SERVE])
        pelo_serve = TestClient(app, client=(f"::ffff:{IP_DO_SERVE}", 40000))
        assert (
            pelo_serve.post(
                "/api/risk/circuit-breaker/reset", headers={"Tailscale-User-Login": LOGIN}
            ).status_code
            == 200
        )

    def test_sem_configuracao_so_o_loopback_e_o_serve(self):
        """Padrao: o `tailscale serve` na mesma maquina conecta de 127.0.0.1."""
        app = FastAPI()

        @app.post("/x")
        async def x():
            return {"ok": True}

        app.add_middleware(PortaoTailscale, login=LOGIN)
        cabecalho = {"Tailscale-User-Login": LOGIN}
        local = TestClient(app, client=("127.0.0.1", 1))
        do_host = TestClient(app, client=(IP_DO_HOST, 1))
        assert local.post("/x", headers=cabecalho).status_code == 200
        assert do_host.post("/x", headers=cabecalho).status_code == 403

    def test_a_sonda_de_saude_do_conteiner_continua_passando(self):
        app, _ = _app([IP_DO_SERVE])
        local = TestClient(app, client=("127.0.0.1", 50000))
        assert local.get("/api/health").status_code == 200
        # Mas o loopback do conteiner nao e o serve: com o login, fora da sonda, 403.
        assert (
            local.post(
                "/api/risk/circuit-breaker/reset", headers={"Tailscale-User-Login": LOGIN}
            ).status_code
            == 403
        )


class TestProxyPorNome:
    """Em producao o proxy e o conteiner `crypto-prd-tailscale`, cujo IP o Docker
    escolhe na subida. O portao resolve o nome pelo DNS da rede e guarda por pouco
    tempo; recriado o conteiner (IP novo), a resolucao e refeita."""

    @pytest.fixture
    def dns(self, monkeypatch):
        tabela = {"crypto-prd-tailscale": frozenset({IP_DO_SERVE})}
        consultas: list[str] = []

        async def resolver(nome: str) -> frozenset[str]:
            consultas.append(nome)
            return tabela.get(nome, frozenset())

        monkeypatch.setattr(modulo_app, "_resolver_nome", resolver)
        return tabela, consultas

    def test_aceita_o_ip_resolvido_e_recusa_o_resto(self, dns):
        app, _ = _app(["crypto-prd-tailscale"])
        cabecalho = {"Tailscale-User-Login": LOGIN}
        pelo_serve = TestClient(app, client=(IP_DO_SERVE, 1))
        do_host = TestClient(app, client=(IP_DO_HOST, 1))
        rota = "/api/risk/circuit-breaker/reset"
        assert pelo_serve.post(rota, headers=cabecalho).status_code == 200
        assert do_host.post(rota, headers=cabecalho).status_code == 403

    def test_conteiner_recriado_com_ip_novo_volta_a_passar(self, dns, monkeypatch):
        tabela, consultas = dns
        relogio = {"agora": 1000.0}
        monkeypatch.setattr(modulo_app, "_agora", lambda: relogio["agora"])
        app, _ = _app(["crypto-prd-tailscale"])
        cabecalho = {"Tailscale-User-Login": LOGIN}

        assert (
            TestClient(app, client=(IP_DO_SERVE, 1))
            .post("/api/risk/circuit-breaker/reset", headers=cabecalho)
            .status_code
            == 200
        )
        tabela["crypto-prd-tailscale"] = frozenset({"172.18.0.9"})
        relogio["agora"] += 10
        novo = TestClient(app, client=("172.18.0.9", 1))
        assert novo.post("/api/risk/circuit-breaker/reset", headers=cabecalho).status_code == 200
        # E o IP antigo, que o Docker pode dar a outro conteiner, deixa de valer.
        relogio["agora"] += 60
        assert (
            TestClient(app, client=(IP_DO_SERVE, 1))
            .post("/api/risk/circuit-breaker/reset", headers=cabecalho)
            .status_code
            == 403
        )
        assert len(consultas) >= 2

    def test_nome_que_nao_resolve_recusa(self, dns):
        app, _ = _app(["conteiner-que-nao-existe"])
        resposta = TestClient(app, client=(IP_DO_SERVE, 1)).post(
            "/api/risk/circuit-breaker/reset", headers={"Tailscale-User-Login": LOGIN}
        )
        assert resposta.status_code == 403


class TestLoginEmBranco:
    def test_login_so_de_espacos_nao_abre_para_quem_vem_sem_cabecalho(self):
        """`ACESSO_LOGIN_TAILSCALE="  "` ligava o portao com login "" -- e o pedido
        SEM cabecalho (login "") casava. O `:?` do compose nao pega espacos."""
        app = FastAPI()

        @app.post("/x")
        async def x():
            return {"ok": True}

        app.add_middleware(PortaoTailscale, login="   ")
        assert TestClient(app, client=("127.0.0.1", 1)).post("/x").status_code == 403


class TestCreateAppLevaAOrigemDoServe:
    async def test_o_proxy_configurado_chega_ao_portao(self, settings):
        producao = settings.model_copy(
            update={
                "acesso_login_tailscale": LOGIN,
                "acesso_proxy_tailscale": [IP_DO_SERVE],
                "cors_origins": ["https://crypto.exemplo.ts.net"],
            }
        )
        from unittest.mock import patch

        # create_app le as settings na criacao.
        with patch("crypto_traders.api.app.get_settings", return_value=producao):
            app = create_app()
        portao = [m for m in app.user_middleware if m.cls is PortaoTailscale]
        assert portao and list(portao[0].kwargs["proxies"]) == [IP_DO_SERVE]


# ---------------------------------------------------------------------------
# Trava de origem
# ---------------------------------------------------------------------------
class TestOrigemEmProducao:
    TS = "https://crypto.exemplo.ts.net"

    @pytest.fixture
    def producao(self, settings):
        return settings.model_copy(
            update={"acesso_login_tailscale": LOGIN, "cors_origins": [self.TS]}
        )

    @pytest.mark.parametrize(
        "origem", ["http://localhost:3000", "http://127.0.0.1:5173", "http://[::1]:8080"]
    )
    def test_loopback_nao_vale_com_o_portao_ligado(self, producao, origem):
        assert origem_confiavel(origem, producao) is False

    def test_a_origem_das_telas_continua_valendo(self, producao):
        assert origem_confiavel(self.TS, producao) is True

    def test_em_desenvolvimento_o_loopback_continua_valendo(self, settings):
        """Sem o portao (notebook, API so em 127.0.0.1) nada muda: o dono abre o
        dashboard por localhost ou 127.0.0.1."""
        assert origem_confiavel("http://127.0.0.1:5173", settings) is True

    async def test_pagina_em_localhost_nao_aciona_rota_com_efeito(self, producao):
        """O cenario do relatorio, de ponta a ponta: o POST simples vindo de uma
        pagina em localhost, com o login do dono posto pelo serve."""
        from unittest.mock import patch

        with patch("crypto_traders.api.app.get_settings", return_value=producao):
            app = create_app()
        app.state.settings = producao
        transporte = ASGITransport(app=app, client=("127.0.0.1", 40000))
        async with AsyncClient(transport=transporte, base_url="http://test") as http:
            resposta = await http.post(
                "/api/agents/resume-all",
                headers={"Origin": "http://localhost:3000", "Tailscale-User-Login": LOGIN},
            )
            assert resposta.status_code == 403
            assert "origem" in resposta.json()["detail"]
            # A origem das telas passa da trava (e cai no 503: sem agentes no teste).
            resposta = await http.post(
                "/api/agents/resume-all",
                headers={"Origin": self.TS, "Tailscale-User-Login": LOGIN},
            )
            assert resposta.status_code == 503
