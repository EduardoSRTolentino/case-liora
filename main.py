import os

import requests
from dotenv import load_dotenv

load_dotenv()


def buscar_solicitacoes(token: str, base_url: str, limit: int) -> list[dict]:
    solicitacoes: list[dict] = []
    offset = 0

    while True:
        response = requests.get(
            f"{base_url}/solicitacoes",
            headers={"Authorization": f"Bearer {token}"},
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


if __name__ == "__main__":
    main()
