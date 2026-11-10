"""Infra de producao (auditoria 09/11/2026): backup, restauracao, aviso de falha, CI e alertas.

Os scripts rodam de verdade, no bash, com `docker`, `curl`, `logger` e `journalctl` de
mentira (o mesmo jeito do `test_vigia.py`): o que se confere e o que eles MANDARAM fazer.
Os arquivos de configuracao (systemd, workflows, regras do Grafana) sao conferidos pelo
conteudo, porque nao ha como roda-los aqui.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
PRODUCAO = RAIZ / "implantacao" / "producao"
BASH = shutil.which("bash")

sem_bash = pytest.mark.skipif(
    BASH is None or "system32" in (BASH or "").lower(),
    reason="precisa de um bash POSIX (Git Bash no Windows, bash no Linux)",
)

FALSO_DOCKER = r"""#!/usr/bin/env bash
echo "docker $* umask=$(umask)" >>"$FALSO_LOG"
case "$*" in
  *"pg_dump"*)
    [ "${FALSO_DUMP_SAIDA:-0}" = 0 ] || exit "$FALSO_DUMP_SAIDA"
    printf 'PGDMP-falso' ;;
  *"pg_restore"*)
    cat >/dev/null
    exit "${FALSO_RESTORE_SAIDA:-0}" ;;
  *"-d crypto_ensaio -c SELECT coalesce"*) echo "${FALSO_TABELAS_ENSAIO:-audit_log,trades}" ;;
  *"-d crypto -c SELECT coalesce"*) echo "${FALSO_TABELAS_CRYPTO:-audit_log,trades}" ;;
  *"pg_trigger"*) echo "${FALSO_GATILHOS:-4}" ;;
  *"count(*) FROM"*) echo 7 ;;
  *"inspect"*) echo "${FALSO_RODANDO:-false}" ;;
esac
exit 0
"""

FALSO_REGISTRO = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >>"$FALSO_LOG"
[ "$(basename "$0")" = curl ] && cat >>"$FALSO_LOG"
[ "$(basename "$0")" = journalctl ] && echo "pg_dump: could not write: No space left on device"
exit 0
"""

FALSO_MKDIR = """#!/usr/bin/env bash
echo "mkdir $* umask=$(umask)" >>"$FALSO_LOG"
PATH=/usr/bin:/bin command mkdir "$@"
"""


@pytest.fixture
def host(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    corpos = {
        "docker": FALSO_DOCKER,
        "curl": FALSO_REGISTRO,
        "logger": FALSO_REGISTRO,
        "journalctl": FALSO_REGISTRO,
        "mkdir": FALSO_MKDIR,
    }
    for nome, corpo in corpos.items():
        alvo = bin_dir / nome
        alvo.write_text(corpo, encoding="utf-8", newline="\n")
        alvo.chmod(0o755)
    pasta = tmp_path / "crypto"
    (pasta / "backups").mkdir(parents=True)
    log = tmp_path / "chamadas.log"
    log.write_text("", encoding="utf-8")

    def rodar(script: str, *args: str, env_arquivo: str = "", **extra: str):
        if env_arquivo is not None and not (extra.get("_sem_env")):
            (pasta / ".env").write_text(
                f"PASTA={pasta.as_posix()}\n{env_arquivo}", encoding="utf-8", newline="\n"
            )
        extra.pop("_sem_env", None)
        antes = log.read_text(encoding="utf-8").splitlines()
        ambiente = {
            **os.environ,
            "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}",
            "PASTA": pasta.as_posix(),
            "FALSO_LOG": log.as_posix(),
            **extra,
        }
        resultado = subprocess.run(
            [BASH, (PRODUCAO / script).as_posix(), *args],
            env=ambiente,
            capture_output=True,
            text=True,
            timeout=60,
        )
        chamadas = log.read_text(encoding="utf-8").splitlines()[len(antes) :]
        return resultado, chamadas

    rodar.pasta = pasta  # type: ignore[attr-defined]
    return rodar


def _com(chamadas: list[str], trecho: str) -> list[str]:
    return [c for c in chamadas if trecho in c]


# ---------------------------------------------------------------------------
# backup.sh
# ---------------------------------------------------------------------------
@sem_bash
class TestBackup:
    def test_dump_nasce_com_umask_077_e_sem_resto_parcial(self, host):
        resto = host.pasta / "backups" / "crypto-20260101-000000.dump.parcial"
        resto.write_text("lixo", encoding="utf-8")

        resultado, chamadas = host("backup.sh", env_arquivo="RESTIC_REPOSITORY=\n")

        assert resultado.returncode == 0, resultado.stderr
        (dump,) = _com(chamadas, "pg_dump")
        # O redirecionamento `>` do dump e feito pelo shell do script: o umask dele vale
        # para o arquivo. Antes era o padrao do root (0022): dump 0644.
        assert dump.endswith("umask=0077"), dump
        dumps = sorted(p.name for p in (host.pasta / "backups").iterdir())
        assert len(dumps) == 1 and dumps[0].endswith(".dump"), dumps

    def test_pg_dump_que_falha_sai_com_erro_e_nao_deixa_parcial(self, host):
        resultado, _ = host("backup.sh", FALSO_DUMP_SAIDA="1")
        assert resultado.returncode != 0
        assert list((host.pasta / "backups").iterdir()) == []

    def test_sem_restic_avisa_que_nao_ha_copia_fora(self, host):
        resultado, chamadas = host("backup.sh", env_arquivo="RESTIC_REPOSITORY=\n")
        assert resultado.returncode == 0, resultado.stderr
        assert "fora do servidor" in resultado.stderr
        assert any("-p user.warning" in c for c in _com(chamadas, "logger"))

    def test_senha_do_restic_nao_vai_na_linha_de_comando(self, host):
        resultado, chamadas = host(
            "backup.sh",
            env_arquivo="RESTIC_REPOSITORY=s3:exemplo/crypto\nRESTIC_PASSWORD=segredo-do-restic\n",
        )
        assert resultado.returncode == 0, resultado.stderr
        execucoes = _com(chamadas, "docker run")
        assert execucoes, chamadas
        assert not any("segredo-do-restic" in c for c in chamadas)
        assert all("RESTIC_PASSWORD_FILE=/run/restic/senha" in c for c in execucoes)


# ---------------------------------------------------------------------------
# preparar.sh
# ---------------------------------------------------------------------------
@sem_bash
class TestPreparar:
    def test_pasta_dos_dumps_nasce_depois_do_umask(self, host, tmp_path):
        nova = tmp_path / "nova"
        resultado, chamadas = host(
            "preparar.sh",
            "--tailnet",
            "exemplo.ts.net",
            "--login",
            "paulo@exemplo.com",
            PASTA=nova.as_posix(),
            _sem_env="1",
        )
        assert resultado.returncode == 0, resultado.stderr
        (criacao,) = _com(chamadas, "mkdir -p")
        assert criacao.endswith("umask=0077"), criacao
        assert (nova / "backup.env").is_file()
        assert "OBRIGATORIO antes do live" in resultado.stdout
        env = (nova / ".env").read_text(encoding="utf-8")
        assert re.search(r"^RESTIC_PASSWORD=\w{20,}$", env, re.M), "senha do restic gerada"


# ---------------------------------------------------------------------------
# avisar-falha.sh e a unidade do systemd
# ---------------------------------------------------------------------------
@sem_bash
class TestAvisoDeFalha:
    def test_com_vigia_url_pinga_fail_com_o_motivo(self, host):
        resultado, chamadas = host(
            "avisar-falha.sh",
            "crypto-backup.service",
            env_arquivo="VIGIA_URL=https://hc.example/abc\n",
        )
        assert resultado.returncode == 0
        (ping,) = _com(chamadas, "curl ")
        assert ping.rstrip().endswith("https://hc.example/abc/fail"), ping
        # O corpo leva o fim do log da unidade (o motivo chega ao dono).
        assert any("No space left on device" in c for c in chamadas)
        assert any("-p user.err" in c for c in _com(chamadas, "logger"))

    def test_sem_vigia_url_so_o_journal(self, host):
        resultado, chamadas = host("avisar-falha.sh", "crypto-backup.service")
        assert resultado.returncode == 0
        assert _com(chamadas, "curl ") == []
        assert any("-p user.err" in c for c in _com(chamadas, "logger"))

    def test_o_backup_aciona_o_aviso_quando_falha(self):
        unidade = (PRODUCAO / "systemd" / "crypto-backup.service").read_text(encoding="utf-8")
        assert re.search(r"^OnFailure=crypto-falha@%n\.service$", unidade, re.M)
        aviso = (PRODUCAO / "systemd" / "crypto-falha@.service").read_text(encoding="utf-8")
        assert "avisar-falha.sh %i" in aviso


# ---------------------------------------------------------------------------
# restaurar.sh
# ---------------------------------------------------------------------------
@sem_bash
class TestRestauracao:
    @pytest.fixture
    def dump(self, host):
        alvo = host.pasta / "backups" / "crypto-20261109-063000.dump"
        alvo.write_text("PGDMP", encoding="utf-8")
        return alvo

    def test_ensaio_restaura_no_descartavel_e_apaga_no_fim(self, host, dump):
        resultado, chamadas = host("restaurar.sh", "--ensaio")
        assert resultado.returncode == 0, resultado.stderr
        assert "ENSAIO OK" in resultado.stdout
        (restauracao,) = _com(chamadas, "pg_restore")
        assert "-d crypto_ensaio" in restauracao
        assert "--single-transaction" in restauracao
        assert "--clean" not in restauracao
        # O banco de producao so e LIDO no ensaio.
        assert not any("-d crypto " in c and "pg_restore" in c for c in chamadas)
        assert "dropdb" in chamadas[-1] and "crypto_ensaio" in chamadas[-1]

    def test_ensaio_com_tabela_faltando_falha_e_apaga_o_descartavel(self, host, dump):
        resultado, chamadas = host(
            "restaurar.sh",
            "--ensaio",
            FALSO_TABELAS_ENSAIO="trades",
            FALSO_TABELAS_CRYPTO="audit_log,trades",
        )
        assert resultado.returncode != 0
        assert "ENSAIO FALHOU" in resultado.stderr
        assert "dropdb" in chamadas[-1] and "crypto_ensaio" in chamadas[-1]

    def test_ensaio_com_restore_que_falha_nao_passa(self, host, dump):
        resultado, chamadas = host("restaurar.sh", "--ensaio", FALSO_RESTORE_SAIDA="1")
        assert resultado.returncode != 0
        assert "ENSAIO OK" not in resultado.stdout
        assert "dropdb" in chamadas[-1]

    def test_sem_confirmar_nao_toca_em_nada(self, host, dump):
        resultado, chamadas = host("restaurar.sh", dump.as_posix())
        assert resultado.returncode == 2
        assert chamadas == []

    def test_de_verdade_para_o_sistema_e_restaura_em_transacao_unica(self, host, dump):
        resultado, chamadas = host("restaurar.sh", "--confirmar", dump.as_posix())
        assert resultado.returncode == 0, resultado.stderr
        ordem = [c.split(" umask")[0] for c in chamadas if c.startswith("docker")]
        i_copia = next(i for i, c in enumerate(ordem) if "pg_dump" in c)
        i_stop = next(i for i, c in enumerate(ordem) if c.startswith("docker stop"))
        i_restore = next(i for i, c in enumerate(ordem) if "pg_restore" in c)
        assert i_copia < i_stop < i_restore
        restauracao = ordem[i_restore]
        assert "-d crypto " in restauracao
        for opcao in ("--clean", "--if-exists", "--single-transaction", "--exit-on-error"):
            assert opcao in restauracao, restauracao
        assert not any(c.startswith("docker start") for c in ordem)
        assert list(host.pasta.glob("backups/antes-de-restaurar-*.dump"))

    def test_sistema_que_nao_para_cancela_a_restauracao(self, host, dump):
        resultado, chamadas = host(
            "restaurar.sh", "--confirmar", dump.as_posix(), FALSO_RODANDO="true"
        )
        assert resultado.returncode != 0
        assert _com(chamadas, "pg_restore") == []


# ---------------------------------------------------------------------------
# Acesso no compose de producao
# ---------------------------------------------------------------------------
class TestComposeDeProducao:
    def test_a_api_so_aceita_o_cabecalho_vindo_do_conteiner_tailscale(self):
        compose = (PRODUCAO / "compose.yml").read_text(encoding="utf-8")
        assert re.search(r"^\s+container_name: crypto-prd-tailscale$", compose, re.M)
        assert re.search(r"^\s+ACESSO_PROXY_TAILSCALE: crypto-prd-tailscale$", compose, re.M)


# ---------------------------------------------------------------------------
# CI: imagem so de commit da main com CI verde; actions fixadas por SHA
# ---------------------------------------------------------------------------
WORKFLOWS = RAIZ / ".github" / "workflows"


class TestWorkflows:
    @pytest.mark.parametrize("nome", ["ci.yml", "imagem.yml"])
    def test_actions_fixadas_por_sha_com_a_versao_em_comentario(self, nome):
        texto = (WORKFLOWS / nome).read_text(encoding="utf-8")
        usos = re.findall(r"uses:\s*(\S+)(.*)$", texto, re.M)
        assert usos
        for alvo, resto in usos:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", alvo), alvo
            assert re.search(r"#\s*v\d+\.\d+\.\d+", resto), f"{alvo}: sem a versao no comentario"

    def test_imagem_so_depois_do_portao_de_ci(self):
        texto = (WORKFLOWS / "imagem.yml").read_text(encoding="utf-8")
        assert re.search(r"^\s+needs:\s*\[?\s*portao", texto, re.M), "o build nao espera o portao"
        # O portao confere o CI do SHA na main e que o commit esta na main.
        assert "actions/workflows/ci.yml/runs" in texto
        assert "merge-base --is-ancestor" in texto


# ---------------------------------------------------------------------------
# Grafana: "sem telemetria" por ambiente
# ---------------------------------------------------------------------------
class TestAlertasDoGrafana:
    @pytest.fixture
    def regras(self):
        dados = json.loads(
            (RAIZ / "observabilidade" / "grafana" / "alertas.json").read_text(encoding="utf-8")
        )
        return {r["uid"]: r for r in dados["regras"]}

    def test_sem_telemetria_agrupa_por_ambiente_dos_dois_lados(self, regras):
        """`max(...) unless max(...)` apagava o rotulo: o dev vivo escondia o prd morto."""
        expr = regras["crypto-sem-telemetria"]["data"][0]["model"]["expr"]
        lados = expr.split(" unless ")
        assert len(lados) == 2
        for lado in lados:
            assert re.match(r"max by \(deployment_environment\)", lado.strip()), lado

    def test_toda_regra_diz_o_ambiente(self, regras):
        for uid, regra in regras.items():
            expr = regra["data"][0]["model"]["expr"]
            assert re.search(r"(max|sum) by \([^)]*deployment_environment", expr), uid
            assert "deployment_environment" in regra["annotations"]["summary"], uid
