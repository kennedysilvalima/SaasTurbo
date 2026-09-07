import re
from datetime import date, timedelta
from decimal import Decimal

import pdfplumber



def ler_texto(caminho):
    with pdfplumber.open(caminho) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def campo_abaixo(caminho, rotulo, padrao, alcance=30, largura=140):
    """Acha o rotulo na pagina e devolve o valor na linha imediatamente abaixo.

    O DANFE e uma tabela: os rotulos ficam numa linha e os valores na de baixo,
    alinhados por coluna. Procurar "o texto depois do rotulo" nao funciona.

    A janela horizontal vai do inicio do rotulo ate a largura da celula, porque
    o valor vem alinhado a direita e nao fica exatamente sob o texto do rotulo.
    """
    tokens = rotulo.split()
    with pdfplumber.open(caminho) as pdf:
        for pagina in pdf.pages:
            palavras = pagina.extract_words()
            for i in range(len(palavras) - len(tokens) + 1):
                janela = palavras[i:i + len(tokens)]
                if [p["text"] for p in janela] != tokens:
                    continue
                x0, base = janela[0]["x0"], janela[-1]["bottom"]
                candidatos = [
                    p for p in palavras
                    if 0 < p["top"] - base < alcance
                    and x0 - 5 <= p["x0"] <= x0 + largura
                    and re.fullmatch(padrao, p["text"])
                ]
                if candidatos:
                    return min(candidatos, key=lambda p: (p["top"], p["x0"]))["text"]
    return None


def extrair_chave(texto):
    """Acha os 44 digitos, com ou sem os espacos que o DANFE insere."""
    limpo = re.sub(r"[ .\-]", "", texto)
    for m in re.finditer(r"\d{44}", limpo):
        if chave_valida(m.group()):
            return m.group()
    return None


def chave_valida(chave):
    """Digito verificador: modulo 11 com pesos 2..9 ciclicos.

    Vale a pena validar: evita confundir a chave com outra sequencia longa
    de digitos que apareca no documento.
    """
    if len(chave) != 44 or not chave.isdigit():
        return False
    pesos = [2, 3, 4, 5, 6, 7, 8, 9]
    soma = sum(int(d) * pesos[i % 8] for i, d in enumerate(reversed(chave[:43])))
    resto = soma % 11
    dv = 0 if resto in (0, 1) else 11 - resto
    return dv == int(chave[43])


def dados_da_chave(chave):
    """A chave carrega a identificacao da nota em posicoes fixas."""
    if not chave or len(chave) != 44:
        return {}
    return {
        "uf": chave[0:2],
        "competencia": f"{chave[4:6]}/20{chave[2:4]}",
        "cnpj_emitente": chave[6:20],
        "modelo": chave[20:22],
        "serie": chave[22:25].lstrip("0") or "0",
        "numero": chave[25:34].lstrip("0"),
    }


BASE_FATOR = date(1997, 10, 7)
VIRADA_FATOR = date(2025, 2, 22)   # fator 9999 voltou para 1000 nesta data


def extrair_linha_digitavel(texto):
    """Boleto bancario: 47 digitos. Aceita a versao com pontos e espacos."""
    padrao = re.compile(
        r"\d{5}[.\s]?\d{5}\s*\d{5}[.\s]?\d{6}\s*\d{5}[.\s]?\d{6}\s*\d\s*\d{14}"
    )
    m = padrao.search(texto)
    if not m:
        return None
    digitos = re.sub(r"\D", "", m.group())
    return digitos if len(digitos) == 47 else None


def dados_do_boleto(linha):
    """Vencimento e valor vem codificados nos ultimos 14 digitos.

    Isso e padrao FEBRABAN: funciona igual em Itau, Bradesco, Caixa, Santander.
    """
    if not linha or len(linha) != 47:
        return {}
    bloco = linha[-14:]
    return {
        "banco": linha[0:3],
        "vencimento": data_do_fator(int(bloco[:4])),
        "valor": Decimal(int(bloco[4:])) / 100,
    }


def data_do_fator(fator):
    """Fator de vencimento: dias desde 07/10/1997, com reinicio em 22/02/2025."""
    if fator == 0:
        return None                                  # boleto sem vencimento
    if fator < 1000:
        return VIRADA_FATOR + timedelta(days=fator)
    antiga = BASE_FATOR + timedelta(days=fator)
    nova = VIRADA_FATOR + timedelta(days=fator - 1000)
    return nova if antiga < VIRADA_FATOR else antiga


def extrair_duplicatas(texto):
    """O bloco FATURA / DUPLICATAS traz vencimento e valor de cada parcela.

    A ordem dos campos varia entre emissores, entao casa as duas listas em vez
    de tentar ler linha a linha.
    """
    inicio = texto.find("FATURA")
    fim = texto.find("CÁLCULO DO IMPOSTO")
    if inicio == -1 or fim == -1 or fim < inicio:
        return []

    bloco = texto[inicio:fim]
    datas = re.findall(r"\b\d{2}/\d{2}/\d{4}\b", bloco)
    valores = re.findall(r"\b\d{1,3}(?:\.\d{3})*,\d{2}\b", bloco)

    # O primeiro valor costuma ser o total da fatura, nao uma parcela.
    if len(valores) == len(datas) + 1:
        valores = valores[1:]

    if not datas or len(datas) != len(valores):
        return []

    return [
        {"vencimento": br_para_data(d), "valor": br_para_decimal(v)}
        for d, v in zip(datas, valores)
    ]


def extrair_valor_total(caminho):
    bruto = campo_abaixo(caminho, "VALOR TOTAL DA NOTA", r"[\d.]+,\d{2}")
    return br_para_decimal(bruto) if bruto else None


def extrair_emissao(caminho):
    bruto = campo_abaixo(caminho, "DATA DA EMISSÃO", r"\d{2}/\d{2}/\d{4}")
    return br_para_data(bruto) if bruto else None


def extrair_emitente(texto):
    """O canhoto do DANFE comeca com "Recebemos de <NOME> CNPJ <numero>".

    E a linha mais confiavel do documento para pegar o fornecedor, porque o
    texto e definido pelo layout oficial e nao pelo emissor.
    """
    m = re.search(
        r"Recebemos de\s+(.+?)\s+CNPJ\s*:?\s*([\d./-]{14,20})", texto, re.IGNORECASE
    )
    if m:
        return {"razao_social": m.group(1).strip(), "cnpj": so_digitos(m.group(2))}
    return {}


def extrair_beneficiario(texto):
    """No boleto, o beneficiario e quem vai receber - ou seja, o fornecedor."""
    m = re.search(r"Benefici\u00e1rio\s*\n?(.+?)\s+(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", texto)
    if m:
        return {"razao_social": m.group(1).strip(), "cnpj": so_digitos(m.group(2))}
    m = re.search(r"([A-Z][A-Z\s.&-]{8,}?(?:LTDA|S/A|S\.A\.|ME|EIRELI))\s+(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", texto)
    if m:
        return {"razao_social": m.group(1).strip(), "cnpj": so_digitos(m.group(2))}
    return {}


def so_digitos(valor):
    return "".join(c for c in (valor or "") if c.isdigit())


def br_para_data(s):
    d, m, a = s.split("/")
    return date(int(a), int(m), int(d))


def br_para_decimal(s):
    return Decimal(s.replace(".", "").replace(",", "."))


def analisar(caminho):
    """Identifica o tipo do PDF e devolve o que conseguiu ler.

    Sempre inclui 'conferir': o que o usuario precisa validar na tela.
    """
    texto = ler_texto(caminho)

    if len(texto.strip()) < 200:
        return {
            "tipo": "sem_texto",
            "aviso": "PDF sem camada de texto (digitalizado) - precisa de OCR",
        }

    chave = extrair_chave(texto)
    if chave:
        duplicatas = extrair_duplicatas(texto)
        return {
            "tipo": "danfe",
            "chave_acesso": chave,
            **dados_da_chave(chave),
            "fornecedor": extrair_emitente(texto),
            "data_emissao": extrair_emissao(caminho),
            "valor_total": extrair_valor_total(caminho),
            "duplicatas": duplicatas,
            "conferir": [] if duplicatas else ["nenhuma parcela encontrada na nota"],
        }

    linha = extrair_linha_digitavel(texto)
    if linha:
        return {
            "tipo": "boleto",
            "linha_digitavel": linha,
            "fornecedor": extrair_beneficiario(texto),
            **dados_do_boleto(linha),
        }

    return {"tipo": "desconhecido", "aviso": "nem DANFE nem boleto reconhecido"}