# Produção do Sistema Cripto

Uma stack só do Crypto (projeto `crypto-prd`), isolada de qualquer outro sistema que more na mesma máquina: rede,
Postgres, nó Tailscale e backup próprios. Desenho e decisões no cofre Obsidian `cryptocoins` (`implantacao.md`,
D32–D39); roteiro completo, inclusive ligar o `live`, em `acessos-e-operacao.md`.

| Arquivo | O que é |
|---|---|
| `compose.yml` | `tailscale` (nó `crypto`, `tailscale serve` no 443), `postgres` (16), `migrar` (roda e sai) e `crypto` |
| `serve.json` | HTTPS do nó → `http://crypto-prd:8000` |
| `preparar.sh` | cria `/opt/crypto/.env` (senhas novas) e `/opt/crypto/crypto.env` (nasce em `dry_run`), modo 600 |
| `implantar.sh` | imagens → Postgres → backup → `migrar` → sistema saudável → confere a trava → nó Tailscale |
| `backup.sh`, `systemd/` | `pg_dump` diário (14 locais) e restic opcional; timer 03:30 |
| `env.exemplo`, `crypto.env.exemplo` | o que cada arquivo da pasta leva |

## Primeira vez, no servidor

```bash
sudo git clone https://github.com/phkolbe/crypto-agentic-traders /opt/crypto/codigo
cd /opt/crypto/codigo
sudo implantacao/producao/preparar.sh --tailnet <rede>.ts.net --login <seu e-mail no Tailscale>
sudo nano /opt/crypto/.env       # VERSAO_CRYPTO, TS_AUTHKEY (só agora), OTEL_ENDPOINT, restic
sudo implantacao/producao/implantar.sh --sem-backup
sudo cp implantacao/producao/systemd/crypto-backup.* /etc/systemd/system/
sudo systemctl enable --now crypto-backup.timer
```

Depois: apagar o `TS_AUTHKEY` do `.env` e desligar a expiração da chave do nó `crypto` no painel do Tailscale.
Endereço: `https://crypto.<tailnet>.ts.net/`, só para `CRYPTO_LOGIN` (a API recusa os outros com 403).

## Acesso

A API não tem login próprio. Aqui ela só é alcançável pelo `tailscale serve`, que põe em cada pedido o
`Tailscale-User-Login` de quem acessa (e sobrescreve o que o cliente mandar); com `ACESSO_LOGIN_TAILSCALE` definido,
a API (`api.app.PortaoTailscale`) recusa qualquer outro login, em HTTP e WebSocket. Nenhuma porta é publicada no
servidor.

## Teste na máquina de desenvolvimento

```bash
docker compose build crypto
PASTA=C:/algum/lugar implantacao/producao/preparar.sh --tailnet exemplo.ts.net --login voce@exemplo.com
sed -i 's/^VERSAO_CRYPTO=$/VERSAO_CRYPTO=local/' C:/algum/lugar/.env
PASTA=C:/algum/lugar implantacao/producao/implantar.sh --local --sem-backup
```

`--local` não baixa imagens nem sobe o nó Tailscale. Desfazer:
`docker compose -p crypto-prd --env-file C:/algum/lugar/.env -f implantacao/producao/compose.yml down -v`.
