"""Agente de avaliação de troca de titularidade (Liora).

Fluxo: lista solicitações, consulta as quatro APIs de apoio, decide com
knockouts + scorecard ponderado e envia cada payload em POST /avaliacoes.

Decisões: aprovado (score_risco 0), analise_manual (1 a 30 ou falha de API),
reprovado (score > 30 ou knockout, que envia score 100).
"""

import calendar
import os
import re
import time
from datetime import date

import requests
from dotenv import load_dotenv

load_dotenv()

MAX_DEBITOS_TENTATIVAS = 3

AGENTE_VERSAO = "v1.1.0-scorecard"

# Pesos do scorecard (somam 100 quando todos os critérios são aplicáveis).
PESO_ENDERECO = 15
PESO_DEBITOS = 28
PESO_TELEFONE = 12
PESO_RECENCIA_CONTA_LUZ = 10
PESO_CONTRATO_LOCACAO = 18
PESO_VINCULO_EMPRESA = 17

LIMITE_SCORE_ANALISE_MANUAL = 30

DIAS_RECENCIA_OK = 180
DIAS_RECENCIA_PARCIAL = 270

FRAUDE_SCORE_OK = 20
FRAUDE_SCORE_PARCIAL = 70

VALORES_TIPO_IMOVEL_ALUGUEL = {"alugado", "locado", "aluguel"}
VALORES_VINCULO_VALIDO = {"true", "1", "sim", "socio", "sócio"}

NOMES_CONSULTAS = {
    "debitos": "débitos da UC",
    "endereco": "validação de endereço",
    "telefone": "validação de telefone",
    "blacklist": "blacklist de CPF",
}


def _headers(token: str) -> dict[str, str]:
    """Monta o header Bearer exigido por todos os endpoints."""
    return {"Authorization": f"Bearer {token}"}


def _retry_after_seconds(response: requests.Response) -> float:
    """Lê o tempo de espera de um 503: campo retry_after do JSON ou header Retry-After."""
    retry_after = None
    try:
        retry_after = response.json().get("retry_after")
    except ValueError:
        pass

    if retry_after is None:
        retry_after = response.headers.get("Retry-After")

    return float(retry_after or 0)


def buscar_solicitacoes(token: str, base_url: str, limit: int) -> list[dict]:
    """Lista todas as solicitações via GET /solicitacoes, paginando até pagination.total."""
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
    """Consulta débitos da UC. Em 503 tenta de novo até 3 vezes, respeitando retry_after."""
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
    """Valida o endereço contra a base da API (CEP, logradouro, cidade, UF)."""
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
    """Consulta se o telefone é VoIP e o score de fraude (0-100)."""
    response = requests.get(
        f"{base_url}/telefone/validar",
        headers=_headers(token),
        params={"telefone": telefone},
    )
    response.raise_for_status()
    return response.json()


def consultar_blacklist_cpf(token: str, base_url: str, cpf: str) -> dict:
    """Consulta blacklist de fraude documental. Aceita CPF ou CNPJ da solicitação."""
    response = requests.get(
        f"{base_url}/cpf/blacklist",
        headers=_headers(token),
        params={"cpf": cpf},
    )
    response.raise_for_status()
    return response.json()


def _consulta_segura(func, *args) -> dict:
    """Executa uma consulta; em qualquer erro devolve {"error": "..."} e não interrompe o lote."""
    try:
        return func(*args)
    except Exception as exc:
        return {"error": str(exc)}


def consultar_solicitacao(token: str, base_url: str, solicitacao: dict) -> dict:
    """Dispara as quatro APIs de apoio e devolve o dict usado por avaliar_solicitacao."""
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


def enviar_avaliacao(token: str, base_url: str, payload: dict) -> dict:
    """Envia a decisão em POST /avaliacoes (idempotente por token + solicitacao_id)."""
    response = requests.post(
        f"{base_url}/avaliacoes",
        headers={**_headers(token), "Content-Type": "application/json"},
        json=payload,
    )
    response.raise_for_status()
    return response.json()


def _parse_data(data_str: str | None) -> date | None:
    """Converte 'YYYY-MM-DD' (ou prefixo ISO) em date; devolve None se vier vazio ou inválido."""
    if not data_str:
        return None
    try:
        return date.fromisoformat(str(data_str)[:10])
    except ValueError:
        return None


def _calcular_idade(data_nascimento: str | None, hoje: date | None = None) -> int | None:
    """Idade em anos completos a partir de data_nascimento, ou None se a data não existir."""
    hoje = hoje or date.today()
    nascimento = _parse_data(data_nascimento)
    if nascimento is None:
        return None
    aniversario_ja_passou = (hoje.month, hoje.day) >= (nascimento.month, nascimento.day)
    return hoje.year - nascimento.year - (0 if aniversario_ja_passou else 1)


def _dias_desde(data_str: str | None, hoje: date | None = None) -> int | None:
    """Dias corridos entre a data informada e hoje, ou None se a data não existir."""
    hoje = hoje or date.today()
    data = _parse_data(data_str)
    if data is None:
        return None
    return (hoje - data).days


def _adicionar_meses(data_base: date, meses: int) -> date:
    """Soma meses a uma data, ajustando o dia se o mês de destino for mais curto."""
    mes_total = data_base.month - 1 + meses
    ano = data_base.year + mes_total // 12
    mes = mes_total % 12 + 1
    dia = min(data_base.day, calendar.monthrange(ano, mes)[1])
    return date(ano, mes, dia)


def _imovel_indica_aluguel(tipo_imovel: str | None) -> bool:
    """True se tipo_imovel for alugado, locado ou aluguel (case-insensitive)."""
    return (tipo_imovel or "").strip().lower() in VALORES_TIPO_IMOVEL_ALUGUEL


def _contrato_vencido(vencimento_str: str | None, hoje: date | None = None) -> bool:
    """True se o vencimento está no passado ou a data está ausente (não aprova às cegas)."""
    hoje = hoje or date.today()
    vencimento = _parse_data(vencimento_str)
    if vencimento is None:
        return True
    return vencimento < hoje


def _digitos_documento(cpf_cnpj: str | None) -> list[int]:
    """Extrai só os dígitos de CPF/CNPJ (ignora pontos, traço e barra)."""
    return [int(c) for c in re.sub(r"\D", "", cpf_cnpj or "")]


def _documento_malformado(cpf_cnpj: str | None, tipo_pessoa: str | None) -> bool:
    """True se o documento não tem formato usável.

    A massa é sintética: dígito verificador errado não reprova. Reprova CPF/CNPJ
    vazio, tamanho errado, todos os dígitos iguais, ou CPF com os dois últimos
    dígitos 00. Tipo de pessoa desconhecido também conta como malformado.
    """
    digitos = _digitos_documento(cpf_cnpj)
    if tipo_pessoa == "PF":
        if len(digitos) != 11 or len(set(digitos)) == 1:
            return True
        return digitos[-2:] == [0, 0]
    if tipo_pessoa == "PJ":
        return len(digitos) != 14 or len(set(digitos)) == 1
    return True


def _score_endereco(endereco: dict) -> float:
    """Subscore 0-1: 1 se válido e CEP consistente; 0.4 se inválido com CEP sugerido; senão 0."""
    valido = endereco.get("valido")
    cep_consistente = endereco.get("cep_consistente")
    if valido and cep_consistente:
        return 1.0
    if valido is False and endereco.get("cep_correto_sugerido"):
        return 0.4
    return 0.0


def _score_debitos(debitos: dict) -> float:
    """Subscore 0-1: 1 regular sem dívida; 0.6 só histórico; 0.3 atraso leve (<= 300); senão 0."""
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
    """Subscore 0-1 pela faixa de fraude_score. VoIP é knockout e não chega aqui."""
    fraude_score = telefone.get("fraude_score")
    if fraude_score is None:
        return 0.0
    if fraude_score <= FRAUDE_SCORE_OK:
        return 1.0
    if fraude_score <= FRAUDE_SCORE_PARCIAL:
        return 0.5
    return 0.0


def _score_recencia_conta_luz(conta_luz_emissao: str | None, hoje: date | None = None) -> float:
    """Subscore 0-1 pela idade da conta: 1 até 180 dias, 0.5 até 270, 0 se mais velha ou ausente."""
    dias = _dias_desde(conta_luz_emissao, hoje)
    if dias is None:
        return 0.0
    if dias <= DIAS_RECENCIA_OK:
        return 1.0
    if dias <= DIAS_RECENCIA_PARCIAL:
        return 0.5
    return 0.0


def _score_contrato_locacao(vencimento_str: str | None, hoje: date | None = None) -> float:
    """Subscore do contrato vigente: 1 se restam >= 6 meses, 0.5 se menos. Vencido já é knockout."""
    hoje = hoje or date.today()
    vencimento = _parse_data(vencimento_str)
    limite = _adicionar_meses(hoje, 6)
    return 1.0 if vencimento is not None and vencimento >= limite else 0.5


def _tem_vinculo_empresa(vinculo_empresa: object | None) -> bool:
    """True para True booleano ou strings socio/sócio/true/sim/1 (formato da API)."""
    if vinculo_empresa is True:
        return True
    if isinstance(vinculo_empresa, str):
        return vinculo_empresa.strip().lower() in VALORES_VINCULO_VALIDO
    return False


def _score_vinculo_empresa(vinculo_empresa: object | None) -> float:
    """Subscore PJ: 1 com vínculo, 0 sem. Critério N/A para PF (nem entra no scorecard)."""
    return 1.0 if _tem_vinculo_empresa(vinculo_empresa) else 0.0


def _consultas_com_falha(consultas: dict) -> list[str]:
    """Nomes amigáveis das consultas que vieram com {"error": "..."}."""
    falhas = []
    for chave, nome in NOMES_CONSULTAS.items():
        dado = consultas.get(chave)
        if isinstance(dado, dict) and "error" in dado:
            falhas.append(nome)
    return falhas


def _verificar_knockouts(solicitacao: dict, consultas: dict, hoje: date | None = None) -> str | None:
    """Primeiro knockout encontrado, ou None. Qualquer um vira reprovado com score 100.

    Ordem: blacklist, documento malformado, VoIP, corte programado, menor de 18,
    aluguel sem contrato vigente ou vencido.
    """
    blacklist = consultas.get("blacklist") or {}
    if blacklist.get("blacklist") is True:
        motivo = blacklist.get("motivo") or "sem motivo informado"
        return f"CPF/CNPJ na blacklist ({motivo})"

    tipo_pessoa = solicitacao.get("tipo_pessoa")
    if _documento_malformado(solicitacao.get("cpf_cnpj"), tipo_pessoa):
        documento = "CPF" if tipo_pessoa == "PF" else "CNPJ"
        digitos = _digitos_documento(solicitacao.get("cpf_cnpj"))
        if tipo_pessoa == "PF" and len(digitos) == 11 and digitos[-2:] == [0, 0]:
            return "CPF com dígitos verificadores 00"
        return f"{documento} com formato inválido"

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
    """Lista (nome, peso, s) dos critérios aplicáveis. Contrato só se alugado; vínculo só se PJ."""
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
    """Score de risco 0-100 (quanto maior, pior). Pesos N/A saem e o restante é renormalizado para 100."""
    criterios = _montar_criterios_scorecard(solicitacao, consultas, hoje)
    peso_total = sum(peso for _, peso, _ in criterios)
    fator = 100 / peso_total if peso_total else 0
    score = sum(peso * fator * (1 - s) for _, peso, s in criterios)
    return round(score), criterios


def _decisao_por_score(score_risco: int) -> str:
    """Corta o score: 0 aprovado, 1-30 analise_manual, acima de 30 reprovado."""
    if score_risco == 0:
        return "aprovado"
    if score_risco <= LIMITE_SCORE_ANALISE_MANUAL:
        return "analise_manual"
    return "reprovado"


def _justificativa_scorecard(score_risco: int, criterios: list[tuple[str, int, float]], decisao: str) -> str:
    """Texto em português citando o score e os critérios com s < 1."""
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
    """Status isolado de identidade: blacklist, formato do documento e idade mínima."""
    blacklist = consultas.get("blacklist") or {}
    if "error" in blacklist:
        return "analise_manual"
    if blacklist.get("blacklist") is True:
        return "reprovado"

    tipo_pessoa = solicitacao.get("tipo_pessoa")
    if _documento_malformado(solicitacao.get("cpf_cnpj"), tipo_pessoa):
        return "reprovado"

    if tipo_pessoa == "PF":
        idade = _calcular_idade(solicitacao.get("data_nascimento"), hoje)
        if idade is not None and idade < 18:
            return "reprovado"

    return "aprovado"


def _verificacao_endereco(consultas: dict) -> str:
    """Status isolado do endereço. Falha da API vira analise_manual; s = 0 vira reprovado."""
    endereco = consultas.get("endereco") or {}
    if "error" in endereco:
        return "analise_manual"
    return "reprovado" if _score_endereco(endereco) == 0 else "aprovado"


def _verificacao_debitos(consultas: dict) -> str:
    """Status isolado dos débitos, incluindo corte programado."""
    debitos = consultas.get("debitos") or {}
    if "error" in debitos:
        return "analise_manual"
    if debitos.get("corte_programado") is True:
        return "reprovado"
    return "reprovado" if _score_debitos(debitos) == 0 else "aprovado"


def _verificacao_telefone(consultas: dict) -> str:
    """Status isolado do telefone, incluindo VoIP."""
    telefone = consultas.get("telefone") or {}
    if "error" in telefone:
        return "analise_manual"
    if telefone.get("voip") is True:
        return "reprovado"
    return "reprovado" if _score_telefone(telefone) == 0 else "aprovado"


def _verificacao_recencia_conta_luz(solicitacao: dict, hoje: date | None = None) -> str:
    """Status isolado da recência da conta de luz."""
    s = _score_recencia_conta_luz(solicitacao.get("conta_luz_emissao"), hoje)
    return "reprovado" if s == 0 else "aprovado"


def _verificacao_contrato_locacao(solicitacao: dict, hoje: date | None = None) -> str:
    """Status isolado do contrato. nao_aplicavel se o imóvel não for alugado."""
    if not _imovel_indica_aluguel(solicitacao.get("tipo_imovel")):
        return "nao_aplicavel"
    if solicitacao.get("contrato_locacao_vigente") is False:
        return "reprovado"
    if _contrato_vencido(solicitacao.get("contrato_locacao_vencimento"), hoje):
        return "reprovado"
    s = _score_contrato_locacao(solicitacao.get("contrato_locacao_vencimento"), hoje)
    return "reprovado" if s == 0 else "aprovado"


def _verificacao_vinculo_empresa(solicitacao: dict) -> str:
    """Status isolado do vínculo. nao_aplicavel para PF."""
    if solicitacao.get("tipo_pessoa") != "PJ":
        return "nao_aplicavel"
    return "aprovado" if _tem_vinculo_empresa(solicitacao.get("vinculo_empresa")) else "reprovado"


def _montar_verificacoes(solicitacao: dict, consultas: dict, hoje: date | None = None) -> dict:
    """Monta o objeto verificacoes do payload: cada chave é o critério isolado, não a decisão final."""
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
    """Decide a solicitação e devolve o corpo de POST /avaliacoes.

    Ordem: falha de API -> analise_manual (score 0); knockout -> reprovado
    (score 100); senão aplica o scorecard ponderado.
    """
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
    """Carrega o .env, percorre as solicitações, avalia e envia cada POST."""
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

    enviadas = 0
    for solicitacao in solicitacoes:
        consultas = consultar_solicitacao(token, base_url, solicitacao)
        payload = avaliar_solicitacao(solicitacao, consultas)
        solicitacao_id = payload["solicitacao_id"]
        try:
            enviar_avaliacao(token, base_url, payload)
        except Exception as exc:
            print(f"{solicitacao_id} erro ao enviar: {exc}")
            continue
        enviadas += 1
        print(
            f"{solicitacao_id} {payload['decisao']} (score {payload['score_risco']})"
        )

    print(f"{enviadas} avaliações enviadas")


if __name__ == "__main__":
    main()
