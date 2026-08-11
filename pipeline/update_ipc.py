#!/usr/bin/env python3
"""
Atualiza data/ipc-anual.json com a taxa de variacao media anual do IPC (dados INE).

Fonte primaria: BPstat (Banco de Portugal), serie 5721550
  "Indice de precos no consumidor (IPC) - total do indice - taxa de variacao
   media dos ultimos 12 meses, valores mensais"
  https://bpstat.bportugal.pt/serie/5721550

O valor de DEZEMBRO de cada ano desta serie corresponde exatamente a taxa de
inflacao media anual publicada pelo INE (o BPstat republica a serie do INE,
que continua a ser a fonte citada na pagina). Usa-se o BPstat porque a API e
estavel e nao muda de codigos com o rebasing do IPC (Base 2025, etc.), ao
contrario dos varcd da API do INE.

Alternativa direta ao INE (se preferires): API json_indicador
  https://www.ine.pt/ine/json_indicador/pindica.jsp?op=2&varcd=<VARCD>&lang=PT
  O <VARCD> do indicador "IPC - Taxa de variacao media anual" deve ser
  confirmado em http://smi.ine.pt (os codigos mudaram com a Base 2025).

Uso: python update_ipc.py
Corre uma vez por ano via GitHub Actions (ver .github/workflows/update-ipc.yml).
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


def fetch_bpstat_december_values() -> dict:
    """Devolve {ano: taxa} com o valor de dezembro de cada ano da serie 5721550."""
    meta_list = get_json(f"{BASE}/series/?series_ids={SERIES_ID}")
    if not meta_list:
        raise RuntimeError("BPstat: metadados da serie nao encontrados")
    dataset_id = meta_list[0]["dataset_id"]

    # Observacoes em JSON-stat
    obs = get_json(f"{BASE}/datasets/{dataset_id}/series/{SERIES_ID}/observations/")

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

    dados = {}
    for periodo, i in index.items():
        v = values[i] if i < len(values) else None
        if v is None:
            continue
        # periodos tipo "1999-12-31", "1999-12" ou "1999M12"
        p = str(periodo)
        ano = p[:4]
        mes = p[5:7] if len(p) >= 7 else ""
        if not ano.isdigit():
            continue
        if "12" in (mes, p[-2:]):  # dezembro
            if int(ano) >= PRIMEIRO_ANO:
                dados[ano] = round(float(v), 1)
    if not dados:
        raise RuntimeError("BPstat: nenhum valor de dezembro encontrado - verificar formato da API")
    return dados


def main() -> int:
    try:
        novos = fetch_bpstat_december_values()
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
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ultimo = max(dados)
    print(f"OK: {len(dados)} anos gravados em {OUT} (ultimo ano: {ultimo} = {dados[ultimo]}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
