import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

MAX_DEBITOS_TENTATIVAS = 3


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _retry_after_seconds(response: requests.Response) -> float:
    retry_after = None
    try:
        retry_after = response.json().get("retry_after")
    except ValueError:
        pass

    if retry_after is None:
        retry_after = response.headers.get("Retry-After")

    return float(retry_after or 0)


def buscar_solicitacoes(token: str, base_url: str, limit: int) -> list[dict]:
    solicitacoes: list[dict] = []
    offset = 0

    while True:
        response = requests.get(
            f"{base_url}/solicitacoes",
            headers=_headers(token),
            params={"limit": limit, "offset": offset},
        )
        response.raise_for_status()

        data = response.json()
        pagina = data.get("solicitacoes") or []
        solicitacoes.extend(pagina)

        pagination = data.get("pagination") or {}
        total = pagination.get("total", 0)
        offset += pagination.get("limit", limit)

        if not pagina or len(pagina) < limit or offset >= total:
            break

    return solicitacoes


def buscar_debitos(token: str, base_url: str, uc: str) -> dict:
    url = f"{base_url}/instalacao/{uc}/debitos"

    for tentativa in range(MAX_DEBITOS_TENTATIVAS):
        response = requests.get(url, headers=_headers(token))
        if response.status_code == 503 and tentativa < MAX_DEBITOS_TENTATIVAS - 1:
            time.sleep(_retry_after_seconds(response))
            continue
        response.raise_for_status()
        return response.json()

    raise RuntimeError(f"Falha ao consultar débitos da UC {uc}")


def validar_endereco(
    token: str,
    base_url: str,
    cep: str,
    logradouro: str,
    cidade: str,
    uf: str,
) -> dict:
    response = requests.get(
        f"{base_url}/endereco/validar",
        headers=_headers(token),
        params={
            "cep": cep,
            "logradouro": logradouro,
            "cidade": cidade,
            "uf": uf,
        },
    )
    response.raise_for_status()
    return response.json()


def validar_telefone(token: str, base_url: str, telefone: str) -> dict:
    response = requests.get(
        f"{base_url}/telefone/validar",
        headers=_headers(token),
        params={"telefone": telefone},
    )
    response.raise_for_status()
    return response.json()


def consultar_blacklist_cpf(token: str, base_url: str, cpf: str) -> dict:
    response = requests.get(
        f"{base_url}/cpf/blacklist",
        headers=_headers(token),
        params={"cpf": cpf},
    )
    response.raise_for_status()
    return response.json()


def _consulta_segura(func, *args) -> dict:
    try:
        return func(*args)
    except Exception as exc:
        return {"error": str(exc)}


def consultar_solicitacao(token: str, base_url: str, solicitacao: dict) -> dict:
    return {
        "solicitacao_id": solicitacao.get("solicitacao_id"),
        "debitos": _consulta_segura(
            buscar_debitos, token, base_url, solicitacao.get("uc")
        ),
        "endereco": _consulta_segura(
            validar_endereco,
            token,
            base_url,
            solicitacao.get("endereco_cep"),
            solicitacao.get("endereco_logradouro"),
            solicitacao.get("endereco_cidade"),
            solicitacao.get("endereco_uf"),
        ),
        "telefone": _consulta_segura(
            validar_telefone, token, base_url, solicitacao.get("telefone")
        ),
        "blacklist": _consulta_segura(
            consultar_blacklist_cpf, token, base_url, solicitacao.get("cpf_cnpj")
        ),
    }


def main() -> None:
    token = os.getenv("TOKEN_API")
    base_url = os.getenv("BASE_URL")
    limit_raw = os.getenv("LIMIT")

    if not token:
        raise RuntimeError("TOKEN_API não encontrado no .env")
    if not base_url:
        raise RuntimeError("BASE_URL não encontrado no .env")
    if not limit_raw:
        raise RuntimeError("LIMIT não encontrado no .env")

    solicitacoes = buscar_solicitacoes(token, base_url, int(limit_raw))
    print(f"{len(solicitacoes)} solicitações carregadas")

    consultas = []
    for solicitacao in solicitacoes:
        resultado = consultar_solicitacao(token, base_url, solicitacao)
        consultas.append(resultado)
        print(solicitacao["solicitacao_id"], "consultada")

    print(f"{len(consultas)} solicitações consultadas")


if __name__ == "__main__":
    main()
