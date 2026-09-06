import calendar
import os
import re
import time
from datetime import date

import requests
from dotenv import load_dotenv

load_dotenv()

MAX_DEBITOS_TENTATIVAS = 3

AGENTE_VERSAO = "v1.0.0-scorecard"

# Pesos do scorecard (somam 100 quando todos os critérios são aplicáveis).
PESO_ENDERECO = 22
PESO_DEBITOS = 22
PESO_TELEFONE = 18
PESO_RECENCIA_CONTA_LUZ = 15
PESO_CONTRATO_LOCACAO = 13
PESO_VINCULO_EMPRESA = 10

VALORES_TIPO_IMOVEL_ALUGUEL = {"alugado", "locado", "aluguel"}

NOMES_CONSULTAS = {
    "debitos": "débitos da UC",
    "endereco": "validação de endereço",
    "telefone": "validação de telefone",
    "blacklist": "blacklist de CPF",
}


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


def _parse_data(data_str: str | None) -> date | None:
    if not data_str:
        return None
    try:
        return date.fromisoformat(str(data_str)[:10])
    except ValueError:
        return None


def _calcular_idade(data_nascimento: str | None, hoje: date | None = None) -> int | None:
    hoje = hoje or date.today()
    nascimento = _parse_data(data_nascimento)
    if nascimento is None:
        return None
    aniversario_ja_passou = (hoje.month, hoje.day) >= (nascimento.month, nascimento.day)
    return hoje.year - nascimento.year - (0 if aniversario_ja_passou else 1)


def _dias_desde(data_str: str | None, hoje: date | None = None) -> int | None:
    hoje = hoje or date.today()
    data = _parse_data(data_str)
    if data is None:
        return None
    return (hoje - data).days


def _adicionar_meses(data_base: date, meses: int) -> date:
    mes_total = data_base.month - 1 + meses
    ano = data_base.year + mes_total // 12
    mes = mes_total % 12 + 1
    dia = min(data_base.day, calendar.monthrange(ano, mes)[1])
    return date(ano, mes, dia)


def _imovel_indica_aluguel(tipo_imovel: str | None) -> bool:
    return (tipo_imovel or "").strip().lower() in VALORES_TIPO_IMOVEL_ALUGUEL


def _contrato_vencido(vencimento_str: str | None, hoje: date | None = None) -> bool:
    hoje = hoje or date.today()
    vencimento = _parse_data(vencimento_str)
    if vencimento is None:
        # Sem data de vencimento não há como confirmar contrato válido:
        # tratado como vencido/inválido para não aprovar às cegas.
        return True
    return vencimento < hoje


def _validar_cpf(cpf: str | None) -> bool:
    digitos = [int(c) for c in re.sub(r"\D", "", cpf or "")]
    if len(digitos) != 11 or len(set(digitos)) == 1:
        return False

    def digito_verificador(base: list[int]) -> int:
        soma = sum(d * peso for d, peso in zip(base, range(len(base) + 1, 1, -1)))
        resto = soma % 11
        return 0 if resto < 2 else 11 - resto

    d1 = digito_verificador(digitos[:9])
    d2 = digito_verificador(digitos[:9] + [d1])
    return digitos[9] == d1 and digitos[10] == d2


def _validar_cnpj(cnpj: str | None) -> bool:
    digitos = [int(c) for c in re.sub(r"\D", "", cnpj or "")]
    if len(digitos) != 14 or len(set(digitos)) == 1:
        return False

    def digito_verificador(base: list[int], pesos: list[int]) -> int:
        soma = sum(d * peso for d, peso in zip(base, pesos))
        resto = soma % 11
        return 0 if resto < 2 else 11 - resto

    pesos_d1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos_d2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = digito_verificador(digitos[:12], pesos_d1)
    d2 = digito_verificador(digitos[:12] + [d1], pesos_d2)
    return digitos[12] == d1 and digitos[13] == d2


def _validar_documento(cpf_cnpj: str | None, tipo_pessoa: str | None) -> bool:
    if tipo_pessoa == "PF":
        return _validar_cpf(cpf_cnpj)
    if tipo_pessoa == "PJ":
        return _validar_cnpj(cpf_cnpj)
    return False


def _score_endereco(endereco: dict) -> float:
    valido = endereco.get("valido")
    cep_consistente = endereco.get("cep_consistente")
    if valido and cep_consistente:
        return 1.0
    if valido is False and endereco.get("cep_correto_sugerido"):
        return 0.4
    return 0.0


def _score_debitos(debitos: dict) -> float:
    status = debitos.get("status")
    total = debitos.get("debitos_total")
    atraso = debitos.get("faturas_em_atraso")
    historico = debitos.get("historico_inadimplencia")

    if status == "regular" and total == 0 and atraso == 0:
        return 0.6 if historico else 1.0
    if atraso and total is not None and total <= 300:
        return 0.3
    return 0.0


def _score_telefone(telefone: dict) -> float:
    fraude_score = telefone.get("fraude_score")
    if fraude_score is None:
        # Dado ausente é tratado como pior caso.
        return 0.0
    return max(0.0, 1 - fraude_score / 100)


def _score_recencia_conta_luz(conta_luz_emissao: str | None, hoje: date | None = None) -> float:
    dias = _dias_desde(conta_luz_emissao, hoje)
    if dias is None:
        return 0.0
    if dias <= 60:
        return 1.0
    if dias <= 120:
        return 0.5
    return 0.0


def _score_contrato_locacao(vencimento_str: str | None, hoje: date | None = None) -> float:
    # Só é chamada quando o contrato já está confirmado vigente e não vencido
    # (caso contrário o knockout K6 já teria disparado antes do scorecard).
    hoje = hoje or date.today()
    vencimento = _parse_data(vencimento_str)
    limite = _adicionar_meses(hoje, 6)
    return 1.0 if vencimento is not None and vencimento >= limite else 0.5


def _score_vinculo_empresa(vinculo_empresa: bool | None) -> float:
    return 1.0 if vinculo_empresa is True else 0.0


def _consultas_com_falha(consultas: dict) -> list[str]:
    falhas = []
    for chave, nome in NOMES_CONSULTAS.items():
        dado = consultas.get(chave)
        if isinstance(dado, dict) and "error" in dado:
            falhas.append(nome)
    return falhas


def _verificar_knockouts(solicitacao: dict, consultas: dict, hoje: date | None = None) -> str | None:
    blacklist = consultas.get("blacklist") or {}
    if blacklist.get("blacklist") is True:
        motivo = blacklist.get("motivo") or "sem motivo informado"
        return f"CPF/CNPJ na blacklist ({motivo})"

    tipo_pessoa = solicitacao.get("tipo_pessoa")
    if not _validar_documento(solicitacao.get("cpf_cnpj"), tipo_pessoa):
        documento = "CPF" if tipo_pessoa == "PF" else "CNPJ"
        return f"{documento} com dígito verificador inválido"

    telefone = consultas.get("telefone") or {}
    if telefone.get("voip") is True:
        return "telefone identificado como VoIP"

    debitos = consultas.get("debitos") or {}
    if debitos.get("corte_programado") is True:
        return "corte de energia programado para a UC"

    if tipo_pessoa == "PF":
        idade = _calcular_idade(solicitacao.get("data_nascimento"), hoje)
        if idade is not None and idade < 18:
            return f"solicitante menor de idade ({idade} anos)"

    if _imovel_indica_aluguel(solicitacao.get("tipo_imovel")):
        if solicitacao.get("contrato_locacao_vigente") is False:
            return "imóvel alugado sem contrato de locação vigente"
        if _contrato_vencido(solicitacao.get("contrato_locacao_vencimento"), hoje):
            return "imóvel alugado com contrato de locação vencido"

    return None


def _montar_criterios_scorecard(solicitacao: dict, consultas: dict, hoje: date | None = None) -> list[tuple[str, int, float]]:
    criterios = [
        ("endereco", PESO_ENDERECO, _score_endereco(consultas.get("endereco") or {})),
        ("debitos_instalacao", PESO_DEBITOS, _score_debitos(consultas.get("debitos") or {})),
        ("telefone", PESO_TELEFONE, _score_telefone(consultas.get("telefone") or {})),
        (
            "recencia_conta_luz",
            PESO_RECENCIA_CONTA_LUZ,
            _score_recencia_conta_luz(solicitacao.get("conta_luz_emissao"), hoje),
        ),
    ]

    if _imovel_indica_aluguel(solicitacao.get("tipo_imovel")):
        criterios.append(
            (
                "contrato_locacao",
                PESO_CONTRATO_LOCACAO,
                _score_contrato_locacao(solicitacao.get("contrato_locacao_vencimento"), hoje),
            )
        )

    if solicitacao.get("tipo_pessoa") == "PJ":
        criterios.append(
            (
                "vinculo_empresa",
                PESO_VINCULO_EMPRESA,
                _score_vinculo_empresa(solicitacao.get("vinculo_empresa")),
            )
        )

    return criterios


def _calcular_scorecard(solicitacao: dict, consultas: dict, hoje: date | None = None) -> tuple[int, list[tuple[str, int, float]]]:
    criterios = _montar_criterios_scorecard(solicitacao, consultas, hoje)
    peso_total = sum(peso for _, peso, _ in criterios)
    fator = 100 / peso_total if peso_total else 0
    score = sum(peso * fator * (1 - s) for _, peso, s in criterios)
    return round(score), criterios


def _decisao_por_score(score_risco: int) -> str:
    if score_risco == 0:
        return "aprovado"
    if score_risco <= 20:
        return "analise_manual"
    return "reprovado"


def _justificativa_scorecard(score_risco: int, criterios: list[tuple[str, int, float]], decisao: str) -> str:
    penalizados = [f"{nome} (s={s:.2f}, peso {peso})" for nome, peso, s in criterios if s < 1]
    if penalizados:
        detalhe = "Critérios que penalizaram o score: " + "; ".join(penalizados) + "."
    else:
        detalhe = "Todos os critérios do scorecard pontuaram no máximo (s=1)."
    return (
        f"Nenhum knockout disparado. Score de risco calculado = {score_risco} "
        f"(0-100), decisão = {decisao}. {detalhe}"
    )


def _verificacao_identidade_documento(solicitacao: dict, consultas: dict, hoje: date | None = None) -> str:
    blacklist = consultas.get("blacklist") or {}
    if "error" in blacklist:
        return "analise_manual"
    if blacklist.get("blacklist") is True:
        return "reprovado"

    tipo_pessoa = solicitacao.get("tipo_pessoa")
    if not _validar_documento(solicitacao.get("cpf_cnpj"), tipo_pessoa):
        return "reprovado"

    if tipo_pessoa == "PF":
        idade = _calcular_idade(solicitacao.get("data_nascimento"), hoje)
        if idade is not None and idade < 18:
            return "reprovado"

    return "aprovado"


def _verificacao_endereco(consultas: dict) -> str:
    endereco = consultas.get("endereco") or {}
    if "error" in endereco:
        return "analise_manual"
    return "reprovado" if _score_endereco(endereco) == 0 else "aprovado"


def _verificacao_debitos(consultas: dict) -> str:
    debitos = consultas.get("debitos") or {}
    if "error" in debitos:
        return "analise_manual"
    if debitos.get("corte_programado") is True:
        return "reprovado"
    return "reprovado" if _score_debitos(debitos) == 0 else "aprovado"


def _verificacao_telefone(consultas: dict) -> str:
    telefone = consultas.get("telefone") or {}
    if "error" in telefone:
        return "analise_manual"
    if telefone.get("voip") is True:
        return "reprovado"
    return "reprovado" if _score_telefone(telefone) == 0 else "aprovado"


def _verificacao_recencia_conta_luz(solicitacao: dict, hoje: date | None = None) -> str:
    s = _score_recencia_conta_luz(solicitacao.get("conta_luz_emissao"), hoje)
    return "reprovado" if s == 0 else "aprovado"


def _verificacao_contrato_locacao(solicitacao: dict, hoje: date | None = None) -> str:
    if not _imovel_indica_aluguel(solicitacao.get("tipo_imovel")):
        return "nao_aplicavel"
    if solicitacao.get("contrato_locacao_vigente") is False:
        return "reprovado"
    if _contrato_vencido(solicitacao.get("contrato_locacao_vencimento"), hoje):
        return "reprovado"
    s = _score_contrato_locacao(solicitacao.get("contrato_locacao_vencimento"), hoje)
    return "reprovado" if s == 0 else "aprovado"


def _verificacao_vinculo_empresa(solicitacao: dict) -> str:
    if solicitacao.get("tipo_pessoa") != "PJ":
        return "nao_aplicavel"
    return "aprovado" if solicitacao.get("vinculo_empresa") is True else "reprovado"


def _montar_verificacoes(solicitacao: dict, consultas: dict, hoje: date | None = None) -> dict:
    return {
        "identidade_documento": _verificacao_identidade_documento(solicitacao, consultas, hoje),
        "endereco": _verificacao_endereco(consultas),
        "debitos_instalacao": _verificacao_debitos(consultas),
        "telefone": _verificacao_telefone(consultas),
        "recencia_conta_luz": _verificacao_recencia_conta_luz(solicitacao, hoje),
        "contrato_locacao": _verificacao_contrato_locacao(solicitacao, hoje),
        "vinculo_empresa": _verificacao_vinculo_empresa(solicitacao),
    }


def avaliar_solicitacao(solicitacao: dict, consultas: dict) -> dict:
    hoje = date.today()
    verificacoes = _montar_verificacoes(solicitacao, consultas, hoje)

    falhas = _consultas_com_falha(consultas)
    if falhas:
        decisao = "analise_manual"
        score_risco = 0
        justificativa = (
            "Análise manual: falha ao consultar "
            + ", ".join(falhas)
            + ". Knockouts e scorecard não podem ser concluídos com segurança "
            "sem esse(s) dado(s), e a falha isolada não é tratada como pior "
            "caso nem gera reprovação automática."
        )
    else:
        motivo_knockout = _verificar_knockouts(solicitacao, consultas, hoje)
        if motivo_knockout:
            decisao = "reprovado"
            score_risco = 100
            justificativa = f"Reprovado por knockout: {motivo_knockout}."
        else:
            score_risco, criterios = _calcular_scorecard(solicitacao, consultas, hoje)
            decisao = _decisao_por_score(score_risco)
            justificativa = _justificativa_scorecard(score_risco, criterios, decisao)

    return {
        "solicitacao_id": solicitacao.get("solicitacao_id"),
        "cpf_cnpj": solicitacao.get("cpf_cnpj"),
        "decisao": decisao,
        "score_risco": score_risco,
        "verificacoes": verificacoes,
        "justificativa": justificativa,
        "agente_versao": AGENTE_VERSAO,
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
