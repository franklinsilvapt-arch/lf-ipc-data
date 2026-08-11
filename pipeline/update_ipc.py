#!/usr/bin/env python3
"""
Atualiza data/ipc-anual.json com a taxa de variacao media anual do IPC (dados INE).

Fonte primaria: BPstat (Banco de Portugal), serie 5721550
  "Indice de precos no consumidor (IPC) - total do indice - taxa de variacao
   media dos ultimos 12 meses, valores mensais"
  https://bpstat.bportugal.pt/serie/5721550

O valor de DEZEMBRO de cada ano desta serie corresponde exatamente a taxa de
inflacao media anual publicada pelo INE (o BPstat republica a serie do INE,
que continua a ser a fonte citada na pagina).

Alem dos anos fechados, o script grava um valor PROVISORIO para o ano corrente:
o ultimo mes disponivel da mesma serie (media dos ultimos 12 meses). Quando o
valor de dezembro e publicado, o ano passa a definitivo e o campo "provisorio"
desaparece automaticamente.

Uso: python update_ipc.py
Corre mensalmente via GitHub Actions (ver .github/workflows/update-ipc.yml).
"""

import json
import sys
import datetime
import urllib.request
from pathlib import Path

SERIES_ID = 5721550
BASE = "https://bpstat.bportugal.pt/data/v1"
OUT = Path(__file__).resolve().parent.parent / "data" / "ipc-anual.json"
PRIMEIRO_ANO = 1960  # inicio da serie usada na calculadora (como o simulador da PORDATA)


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "LiteraciaFinanceira-ipc-updater/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_bpstat_values():
    """Devolve (dados, provisorio):
    - dados: {ano: taxa} com o valor de dezembro de cada ano (= media anual do INE)
    - provisorio: (ano, mes, taxa) do ultimo mes disponivel do ano corrente,
      ou None se dezembro desse ano ja tiver sido publicado."""
    meta_list = get_json(f"{BASE}/series/?series_ids={SERIES_ID}")
    if not meta_list:
        raise RuntimeError("BPstat: metadados da serie nao encontrados")
    dataset_id = meta_list[0]["dataset_id"]
    domain_id = meta_list[0]["domain_ids"][0]

    # Observacoes em JSON-stat 2.0. A serie e devolvida dentro do dataset do
    # dominio; o endpoint /datasets/<id>/series/<id>/observations/ devolve 404.
    obs = get_json(f"{BASE}/domains/{domain_id}/datasets/{dataset_id}/?series_ids={SERIES_ID}")

    # Encontrar a dimensao temporal (JSON-stat 2.0)
    dims = obs.get("dimension", {})
    dim_ids = obs.get("id", list(dims.keys()))
    time_dim = None
    for d in dim_ids:
        if "time" in d.lower() or "period" in d.lower() or "date" in d.lower():
            time_dim = d
            break
    if time_dim is None:
        # fallback: dimensao com mais categorias
        time_dim = max(dim_ids, key=lambda d: len(dims[d]["category"]["index"]))

    index = dims[time_dim]["category"]["index"]  # {"1999-12-31": 0, ...} ou lista
    if isinstance(index, list):
        index = {k: i for i, k in enumerate(index)}
    values = obs["value"]
    if isinstance(values, dict):
        values = {int(k): v for k, v in values.items()}
        values = [values.get(i) for i in range(max(values) + 1)]

    mensal = {}  # {(ano, mes): taxa}
    for periodo, i in index.items():
        v = values[i] if i < len(values) else None
        if v is None:
            continue
        # periodos tipo "1999-12-31", "1999-12" ou "1999M12"
        p = str(periodo)
        ano_s = p[:4]
        if not ano_s.isdigit():
            continue
        mes_s = p[5:7] if len(p) >= 7 and p[5:7].isdigit() else p[-2:]
        if not mes_s.isdigit():
            continue
        ano, mes = int(ano_s), int(mes_s)
        if 1 <= mes <= 12 and ano >= PRIMEIRO_ANO:
            mensal[(ano, mes)] = float(v)

    if not mensal:
        raise RuntimeError("BPstat: nenhum valor mensal encontrado - verificar formato da API")

    dados = {str(a): round(v, 1) for (a, m), v in mensal.items() if m == 12}
    if not dados:
        raise RuntimeError("BPstat: nenhum valor de dezembro encontrado")

    ano_corrente = datetime.date.today().year
    provisorio = None
    if str(ano_corrente) not in dados:
        meses = sorted(m for (a, m) in mensal if a == ano_corrente)
        if meses:
            m = meses[-1]
            provisorio = (ano_corrente, m, round(mensal[(ano_corrente, m)], 1))
    return dados, provisorio


def main() -> int:
    try:
        novos, prov = fetch_bpstat_values()
    except Exception as e:
        print(f"ERRO ao obter dados do BPstat: {e}", file=sys.stderr)
        return 1

    existentes = {}
    if OUT.exists():
        existentes = json.loads(OUT.read_text(encoding="utf-8")).get("dados", {})

    # Os novos valores prevalecem; anos antigos sem valor novo mantem-se
    dados = {**existentes, **novos}
    dados = {a: dados[a] for a in sorted(dados)}

    payload = {
        "indicador": "IPC - taxa de variacao media anual (%)",
        "fonte": "INE - Instituto Nacional de Estatistica (via BPstat, serie 5721550)",
        "atualizado": datetime.date.today().isoformat(),
        "dados": dados,
    }
    if prov:
        ano, mes, taxa = prov
        payload["provisorio"] = {str(ano): taxa}
        payload["provisorio_mes"] = f"{ano}-{mes:02d}"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ultimo = max(dados)
    msg = f"OK: {len(dados)} anos fechados (ultimo: {ultimo} = {dados[ultimo]}%)"
    if prov:
        msg += f" | provisorio {prov[0]} = {prov[2]}% (ate {prov[0]}-{prov[1]:02d})"
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
