import csv
import io
import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from sqlalchemy import func

import extratores
from models import (Empresa, Fornecedor, NotaFiscal, Papel, Projeto,
                    SolicitacaoCancelamento, StatusNota, StatusSolicitacao,
                    StatusTitulo, Titulo, Usuario, db)

BASE = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "troque-isto-em-producao")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///" + os.path.join(BASE, "contas.sqlite3")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Entre para continuar."


@login_manager.user_loader
def carregar_usuario(user_id):
    return db.session.get(Usuario, int(user_id))


def da_empresa(modelo):
    return modelo.query.filter_by(empresa_id=current_user.empresa_id)


def _titulos_validos():
    return da_empresa(Titulo).join(NotaFiscal).filter(
        Titulo.status != StatusTitulo.CANCELADO,
        NotaFiscal.status != StatusNota.CANCELADA,
    )


def buscar_ou_404(modelo, id_):
    obj = da_empresa(modelo).filter_by(id=id_).first()
    if obj is None:
        abort(404)
    return obj


def somente_admin(funcao):
    from functools import wraps

    @wraps(funcao)
    def interna(*args, **kwargs):
        if current_user.papel != Papel.ADMIN:
            abort(403)
        return funcao(*args, **kwargs)
    return interna


def paginar(consulta, por_pagina=40):
    pagina = max(1, request.args.get("pagina", 1, type=int))
    total = consulta.count()
    itens = consulta.limit(por_pagina).offset((pagina - 1) * por_pagina).all()
    ultima = max(1, -(-total // por_pagina))
    return itens, {"atual": pagina, "ultima": ultima, "total": total}


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("painel"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        usuario = Usuario.query.filter_by(email=email, ativo=True).first()

        if usuario and usuario.conferir_senha(senha):
            login_user(usuario)
            return redirect(request.args.get("next") or url_for("painel"))


        flash("E-mail ou senha incorretos.", "erro")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def painel():
    hoje = date.today()
    em_aberto = _titulos_validos().filter(Titulo.status == StatusTitulo.ABERTO)

    def somar(consulta):
        return consulta.with_entities(
            func.coalesce(func.sum(Titulo.valor), 0)
        ).scalar() or Decimal("0.00")

    vencidos = em_aberto.filter(Titulo.vencimento < hoje)
    vence_hoje = em_aberto.filter(Titulo.vencimento == hoje)
    proximos = em_aberto.filter(
        Titulo.vencimento > hoje, Titulo.vencimento <= hoje + timedelta(days=7)
    )

    pago_mes = _titulos_validos().filter(
        Titulo.status == StatusTitulo.PAGO,
        Titulo.data_pagamento >= hoje.replace(day=1),
    )

    return render_template(
        "painel.html",
        hoje=hoje,
        total_vencido=somar(vencidos),
        qtd_vencido=vencidos.count(),
        total_hoje=somar(vence_hoje),
        qtd_hoje=vence_hoje.count(),
        total_semana=somar(proximos),
        qtd_semana=proximos.count(),
        total_pago_mes=pago_mes.with_entities(
            func.coalesce(func.sum(Titulo.valor_pago), 0)
        ).scalar() or Decimal("0.00"),
        urgentes=vencidos.order_by(Titulo.vencimento).limit(8).all(),
        sem_parcelas=[
            n for n in da_empresa(NotaFiscal)
            .filter_by(status=StatusNota.PENDENTE).all() if n.sem_titulos
        ],
    )


FILTROS = {
    "abertos": "Em aberto",
    "vencidos": "Vencidos",
    "semana": "Vencem em 7 dias",
    "pagos": "Pagos",
    "todos": "Todos",
}


@app.route("/titulos")
@login_required
def titulos():
    filtro = request.args.get("filtro", "abertos")
    busca = request.args.get("busca", "").strip()
    hoje = date.today()
    consulta = da_empresa(Titulo).join(NotaFiscal).join(Fornecedor)

    if filtro != "todos":
        consulta = consulta.filter(
            Titulo.status != StatusTitulo.CANCELADO,
            NotaFiscal.status != StatusNota.CANCELADA,
        )

    if filtro == "abertos":
        consulta = consulta.filter(Titulo.status == StatusTitulo.ABERTO)
    elif filtro == "vencidos":
        consulta = consulta.filter(Titulo.status == StatusTitulo.ABERTO,
                                   Titulo.vencimento < hoje)
    elif filtro == "semana":
        consulta = consulta.filter(Titulo.status == StatusTitulo.ABERTO,
                                   Titulo.vencimento <= hoje + timedelta(days=7))
    elif filtro == "pagos":
        consulta = consulta.filter(Titulo.status == StatusTitulo.PAGO)

    if busca:
        alvo = f"%{busca}%"
        consulta = consulta.filter(
            db.or_(Fornecedor.razao_social.ilike(alvo), NotaFiscal.numero.ilike(alvo))
        )

    ordem = Titulo.data_pagamento.desc() if filtro == "pagos" else Titulo.vencimento
    itens, pagina = paginar(consulta.order_by(ordem))

    return render_template("titulos.html", titulos=itens, filtro=filtro,
                           filtros=FILTROS, hoje=hoje, busca=busca, pagina=pagina)


@app.route("/titulos/<int:id_>/baixar", methods=["POST"])
@login_required
def baixar_titulo(id_):
    titulo = buscar_ou_404(Titulo, id_)
    try:


        informado = request.form.get("valor_pago")
        titulo.baixar(
            usuario=current_user,
            data_pagamento=_data(request.form.get("data_pagamento")) or date.today(),
            valor_pago=_decimal(informado) if informado else titulo.valor,
            juros=_decimal(request.form.get("juros")),
            multa=_decimal(request.form.get("multa")),
            desconto=_decimal(request.form.get("desconto")),
        )
        db.session.commit()
        flash(f"Título de {_moeda(titulo.valor)} baixado.", "ok")
    except ValueError as e:
        flash(str(e), "erro")
    return redirect(request.referrer or url_for("titulos"))


@app.route("/titulos/<int:id_>/estornar", methods=["POST"])
@login_required
def estornar_titulo(id_):
    titulo = buscar_ou_404(Titulo, id_)
    motivo = request.form.get("motivo", "").strip()
    if not motivo:
        flash("Informe o motivo do estorno.", "erro")
        return redirect(request.referrer or url_for("titulos"))
    try:
        titulo.estornar(current_user, motivo)
        db.session.commit()
        flash("Pagamento estornado. O título voltou para em aberto.", "ok")
    except ValueError as e:
        flash(str(e), "erro")
    return redirect(request.referrer or url_for("titulos"))


@app.route("/notas")
@login_required
def notas():
    busca = request.args.get("busca", "").strip()
    consulta = da_empresa(NotaFiscal).join(Fornecedor)
    if busca:
        alvo = f"%{busca}%"
        consulta = consulta.filter(
            db.or_(Fornecedor.razao_social.ilike(alvo),
                   NotaFiscal.numero.ilike(alvo),
                   NotaFiscal.chave_acesso.ilike(alvo))
        )
    itens, pagina = paginar(consulta.order_by(NotaFiscal.data_emissao.desc()))
    return render_template("notas.html", notas=itens, busca=busca, pagina=pagina)


@app.route("/notas/nova", methods=["GET", "POST"])
@login_required
def nova_nota():
    fornecedores = da_empresa(Fornecedor).filter_by(ativo=True).order_by(
        Fornecedor.razao_social).all()

    if request.method == "POST":
        chave = _so_digitos(request.form.get("chave_acesso"))
        if chave and da_empresa(NotaFiscal).filter_by(chave_acesso=chave).first():
            flash("Esta nota já foi lançada.", "erro")
            return render_template("nota_form.html", fornecedores=fornecedores,
                                   projetos=_projetos_ativos(), dados=request.form)

        nota = NotaFiscal(
            empresa_id=current_user.empresa_id,
            fornecedor_id=int(request.form["fornecedor_id"]),
            numero=request.form["numero"].strip(),
            serie=request.form.get("serie", "").strip() or None,
            chave_acesso=chave or None,
            data_emissao=_data(request.form["data_emissao"]),
            valor_total=_decimal(request.form["valor_total"]),
            tipo=request.form.get("tipo"),
            projeto=request.form.get("projeto", "").strip() or None,
            descricao=request.form.get("descricao", "").strip() or None,
            criada_por_id=current_user.id,
        )
        db.session.add(nota)
        db.session.flush()

        for parcela in gerar_parcelas(
            valor_total=nota.valor_total,
            quantidade=int(request.form.get("qtd_parcelas") or 1),
            primeiro_vencimento=_data(request.form["primeiro_vencimento"]),
            intervalo=int(request.form.get("intervalo") or 30),
        ):
            db.session.add(Titulo(
                empresa_id=current_user.empresa_id,
                nota_fiscal_id=nota.id,
                forma_pagamento=request.form.get("forma_pagamento"),
                **parcela,
            ))

        db.session.commit()
        flash(f"Nota {nota.numero} lançada.", "ok")
        return redirect(url_for("notas"))

    return render_template("nota_form.html", fornecedores=fornecedores,
                           projetos=_projetos_ativos(), dados={})


@app.route("/notas/<int:id_>/cancelar", methods=["POST"])
@login_required
def cancelar_nota(id_):
    if current_user.papel != Papel.ADMIN:
        abort(403)
    nota = buscar_ou_404(NotaFiscal, id_)
    motivo = request.form.get("motivo", "").strip()
    if not motivo:
        flash("Informe o motivo do cancelamento.", "erro")
    else:
        nota.cancelar(current_user, motivo)
        db.session.commit()
        flash("Nota cancelada. Os títulos em aberto também foram cancelados.", "ok")
    return redirect(url_for("notas"))


def gerar_parcelas(valor_total, quantidade, primeiro_vencimento, intervalo=30):
    quantidade = max(1, quantidade)
    base = (valor_total / quantidade).quantize(Decimal("0.01"), ROUND_HALF_UP)

    parcelas = []
    for i in range(quantidade):
        valor = base if i < quantidade - 1 else valor_total - base * (quantidade - 1)
        parcelas.append({
            "numero_parcela": i + 1,
            "total_parcelas": quantidade,
            "vencimento": primeiro_vencimento + timedelta(days=intervalo * i),
            "valor": valor,
        })
    return parcelas


@app.route("/importar")
@login_required
def importar():
    fornecedores = da_empresa(Fornecedor).filter_by(ativo=True).order_by(
        Fornecedor.razao_social).all()
    return render_template("importar.html", fornecedores=fornecedores,
                           projetos=_projetos_ativos())


@app.route("/importar/ler", methods=["POST"])
@login_required
def ler_pdfs():
    esperado = request.args.get("tipo", "danfe")
    leituras = []

    for enviado in request.files.getlist("arquivos"):
        if not enviado.filename.lower().endswith(".pdf"):
            leituras.append({"tipo": "invalido", "arquivo": enviado.filename})
            continue
        leituras.append({"arquivo": enviado.filename, **_ler_em_memoria(enviado)})

    return jsonify(_organizar(leituras, esperado))


def _ler_em_memoria(enviado):
    caminho = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            enviado.save(tmp)
            caminho = tmp.name
        return extratores.analisar(caminho)
    except Exception:
        return {"tipo": "erro", "aviso": "não foi possível ler este arquivo"}
    finally:
        if caminho and os.path.exists(caminho):
            os.remove(caminho)


ROTULOS = {"danfe": "nota fiscal", "boleto": "boleto"}
ARTIGOS = {"danfe": "uma nota fiscal", "boleto": "um boleto"}


def _organizar(leituras, esperado):
    avisos = []
    nota, parcelas, fornecedor = {}, [], {}

    for leitura in leituras:
        arquivo = leitura.get("arquivo", "arquivo")
        tipo = leitura.get("tipo")

        if tipo in ("sem_texto", "erro", "desconhecido", "invalido"):
            avisos.append(f"{arquivo}: {leitura.get('aviso', 'não reconhecido')}")
            continue

        if tipo != esperado:
            avisos.append(
                f"{arquivo} parece ser {ARTIGOS.get(tipo, tipo)} e foi solto na área "
                f"de {ROTULOS.get(esperado, esperado)}. Use a outra área.")
            continue

        if not fornecedor:
            fornecedor = leitura.get("fornecedor") or {}

        if tipo == "danfe":
            nota = {
                "numero": leitura.get("numero"),
                "serie": leitura.get("serie"),
                "chave_acesso": leitura.get("chave_acesso"),
                "data_emissao": _iso(leitura.get("data_emissao")),
                "valor_total": _texto_decimal(leitura.get("valor_total")),
            }
            parcelas = [
                {"vencimento": _iso(d["vencimento"]),
                 "valor": _texto_decimal(d["valor"])}
                for d in leitura.get("duplicatas", [])
            ]
            if not parcelas:
                avisos.append(
                    f"{arquivo}: a nota não traz as parcelas. Importe os boletos ou "
                    f"informe os vencimentos à mão.")

            if nota["chave_acesso"] and da_empresa(NotaFiscal).filter_by(
                    chave_acesso=nota["chave_acesso"]).first():
                avisos.append("Esta nota já foi lançada antes.")

        else:
            parcelas.append({
                "vencimento": _iso(leitura.get("vencimento")),
                "valor": _texto_decimal(leitura.get("valor")),
                "linha_digitavel": leitura.get("linha_digitavel"),
            })

    if fornecedor.get("cnpj"):
        existente = _fornecedor_por_cnpj(fornecedor["cnpj"])
        fornecedor["id"] = existente.id if existente else None
        if not existente:
            avisos.append(
                f"Fornecedor {fornecedor.get('razao_social', '')} ainda não cadastrado. "
                f"Os dados foram preenchidos abaixo.")

    parcelas.sort(key=lambda p: p["vencimento"] or "")
    return {"tipo": esperado, "nota": nota, "parcelas": parcelas,
            "fornecedor": fornecedor, "avisos": avisos}


def _fornecedor_por_cnpj(cnpj):
    exato = da_empresa(Fornecedor).filter_by(cnpj=cnpj).first()
    if exato:
        return exato
    return da_empresa(Fornecedor).filter(Fornecedor.cnpj.like(cnpj[:8] + "%")).first()


@app.route("/importar/salvar", methods=["POST"])
@login_required
def salvar_importada():
    fornecedor_id = request.form.get("fornecedor_id")

    if not fornecedor_id:
        razao = request.form.get("nova_razao_social", "").strip()
        cnpj = _so_digitos(request.form.get("novo_cnpj"))
        if not razao or not cnpj:
            flash("Escolha um fornecedor ou informe razão social e CNPJ.", "erro")
            return redirect(url_for("importar"))
        novo = Fornecedor(empresa_id=current_user.empresa_id,
                          razao_social=razao, cnpj=cnpj)
        db.session.add(novo)
        db.session.flush()
        fornecedor_id = novo.id

    chave = _so_digitos(request.form.get("chave_acesso"))
    if chave and da_empresa(NotaFiscal).filter_by(chave_acesso=chave).first():
        flash("Esta nota já foi lançada.", "erro")
        return redirect(url_for("importar"))

    nota = NotaFiscal(
        empresa_id=current_user.empresa_id,
        fornecedor_id=int(fornecedor_id),
        numero=request.form["numero"].strip(),
        serie=request.form.get("serie", "").strip() or None,
        chave_acesso=chave or None,
        data_emissao=_data(request.form["data_emissao"]),
        valor_total=_decimal(request.form["valor_total"]),
        tipo=request.form.get("tipo"),
        projeto=request.form.get("projeto", "").strip() or None,
        criada_por_id=current_user.id,
    )
    db.session.add(nota)
    db.session.flush()

    vencimentos = request.form.getlist("parcela_vencimento")
    valores = request.form.getlist("parcela_valor")
    linhas = request.form.getlist("parcela_linha")
    total = len(vencimentos)

    for i, (venc, valor) in enumerate(zip(vencimentos, valores), start=1):
        if not venc:
            continue
        db.session.add(Titulo(
            empresa_id=current_user.empresa_id,
            nota_fiscal_id=nota.id,
            numero_parcela=i,
            total_parcelas=total,
            vencimento=_data(venc),
            valor=_decimal(valor),
            linha_digitavel=(linhas[i - 1] if i - 1 < len(linhas) else "") or None,
            forma_pagamento=request.form.get("forma_pagamento"),
        ))

    db.session.commit()
    flash(f"Nota {nota.numero} lançada com {total} parcela(s).", "ok")
    return redirect(url_for("notas"))


def _iso(valor):
    return valor.isoformat() if hasattr(valor, "isoformat") else (valor or "")


def _texto_decimal(valor):
    return f"{Decimal(valor):.2f}" if valor is not None else ""


@app.route("/fornecedores", methods=["GET", "POST"])
@login_required
def fornecedores():
    if request.method == "POST":
        cnpj = _so_digitos(request.form.get("cnpj"))
        if da_empresa(Fornecedor).filter_by(cnpj=cnpj).first():
            flash("Já existe fornecedor com este CNPJ.", "erro")
        else:
            db.session.add(Fornecedor(
                empresa_id=current_user.empresa_id,
                razao_social=request.form["razao_social"].strip(),
                nome_fantasia=request.form.get("nome_fantasia", "").strip() or None,
                cnpj=cnpj,
                contato=request.form.get("contato", "").strip() or None,
                email=request.form.get("email", "").strip() or None,
                telefone=request.form.get("telefone", "").strip() or None,
            ))
            db.session.commit()
            flash("Fornecedor cadastrado.", "ok")
        return redirect(url_for("fornecedores"))

    lista = da_empresa(Fornecedor).order_by(
        Fornecedor.ativo.desc(), Fornecedor.razao_social).all()
    return render_template("fornecedores.html", fornecedores=lista)


@app.route("/notas/<int:id_>/editar", methods=["GET", "POST"])
@login_required
def editar_nota(id_):
    nota = buscar_ou_404(NotaFiscal, id_)

    if nota.status == StatusNota.CANCELADA:
        flash("Nota cancelada não pode ser editada.", "erro")
        return redirect(url_for("notas"))

    if request.method == "POST":
        nota.numero = request.form["numero"].strip()
        nota.serie = request.form.get("serie", "").strip() or None
        nota.data_emissao = _data(request.form["data_emissao"])
        nota.projeto = request.form.get("projeto") or None
        nota.tipo = request.form.get("tipo")
        nota.descricao = request.form.get("descricao", "").strip() or None


        if not nota.tem_pagamento:
            nota.valor_total = _decimal(request.form["valor_total"])
            for titulo in nota.titulos:
                if titulo.status == StatusTitulo.ABERTO:
                    venc = request.form.get(f"venc_{titulo.id}")
                    valor = request.form.get(f"valor_{titulo.id}")
                    if venc:
                        titulo.vencimento = _data(venc)
                    if valor:
                        titulo.valor = _decimal(valor)
                    titulo.linha_digitavel = request.form.get(f"linha_{titulo.id}") or None

        db.session.commit()
        flash("Nota atualizada.", "ok")
        return redirect(url_for("notas"))

    return render_template("nota_editar.html", nota=nota,
                           projetos=_projetos_ativos())


@app.route("/titulos/<int:id_>/cancelar", methods=["POST"])
@login_required
def cancelar_titulo(id_):
    titulo = buscar_ou_404(Titulo, id_)
    if titulo.status != StatusTitulo.ABERTO:
        flash("Só é possível cancelar título em aberto.", "erro")
    else:
        titulo.status = StatusTitulo.CANCELADO
        titulo.observacao = (request.form.get("motivo") or "cancelado").strip()
        db.session.commit()
        flash("Título cancelado.", "ok")
    return redirect(request.referrer or url_for("titulos"))


def _projetos_ativos():
    return da_empresa(Projeto).filter_by(ativo=True).order_by(Projeto.nome).all()


@app.route("/projetos", methods=["GET", "POST"])
@login_required
def projetos():
    if request.method == "POST":
        if current_user.papel != Papel.ADMIN:
            abort(403)

        nome = request.form.get("nome", "").strip()
        unidades = request.form.get("unidades", type=int)

        if not nome:
            flash("Informe o nome do projeto.", "erro")
        elif unidades is None or unidades < 0:
            flash("Informe a quantidade de unidades.", "erro")
        elif da_empresa(Projeto).filter_by(nome=nome).first():
            flash("Já existe um projeto com esse nome.", "erro")
        else:
            db.session.add(Projeto(
                empresa_id=current_user.empresa_id,
                nome=nome,
                unidades=unidades,
            ))
            db.session.commit()
            flash(f"Projeto {nome} cadastrado.", "ok")
        return redirect(url_for("projetos"))

    lista = da_empresa(Projeto).order_by(Projeto.ativo.desc(), Projeto.nome).all()
    return render_template("projetos.html", projetos=lista)


@app.route("/projetos/<int:id_>/editar", methods=["POST"])
@login_required
@somente_admin
def editar_projeto(id_):
    projeto = buscar_ou_404(Projeto, id_)
    nome = request.form.get("nome", "").strip()
    unidades = request.form.get("unidades", type=int)

    outro = da_empresa(Projeto).filter(
        Projeto.nome == nome, Projeto.id != projeto.id).first()

    if not nome or unidades is None or unidades < 0:
        flash("Nome e quantidade de unidades são obrigatórios.", "erro")
    elif outro:
        flash("Já existe outro projeto com esse nome.", "erro")
    else:
        anterior = projeto.nome
        projeto.nome = nome
        projeto.unidades = unidades
        if anterior != nome:
            for nota in da_empresa(NotaFiscal).filter_by(projeto=anterior).all():
                nota.projeto = nome
        db.session.commit()
        flash("Projeto atualizado.", "ok")

    return redirect(url_for("projetos"))


@app.route("/projetos/<int:id_>/alternar", methods=["POST"])
@login_required
@somente_admin
def alternar_projeto(id_):
    projeto = buscar_ou_404(Projeto, id_)
    projeto.ativo = not projeto.ativo
    db.session.commit()
    flash(f"Projeto {'reaberto' if projeto.ativo else 'concluído'}.", "ok")
    return redirect(url_for("projetos"))


@app.route("/usuarios", methods=["GET", "POST"])
@login_required
@somente_admin
def usuarios():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")

        if len(senha) < 8:
            flash("A senha precisa ter ao menos 8 caracteres.", "erro")
        elif Usuario.query.filter_by(email=email).first():
            flash("Já existe usuário com esse e-mail.", "erro")
        else:
            novo = Usuario(
                empresa_id=current_user.empresa_id,
                nome=request.form.get("nome", "").strip(),
                email=email,
                papel=request.form.get("papel", Papel.OPERADOR),
            )
            novo.definir_senha(senha)
            db.session.add(novo)
            db.session.commit()
            flash(f"Usuário {novo.nome} cadastrado.", "ok")
        return redirect(url_for("usuarios"))

    lista = da_empresa(Usuario).order_by(Usuario.ativo.desc(), Usuario.nome).all()
    historicos = {u.id: sum(_historico_do_usuario(u).values()) for u in lista}
    return render_template("usuarios.html", usuarios=lista, papeis=PAPEIS,
                           historicos=historicos)


@app.route("/usuarios/<int:id_>/alternar", methods=["POST"])
@login_required
@somente_admin
def alternar_usuario(id_):
    usuario = buscar_ou_404(Usuario, id_)
    if usuario.id == current_user.id:
        flash("Você não pode desativar o próprio acesso.", "erro")
    else:
        usuario.ativo = not usuario.ativo
        db.session.commit()
        flash(f"Acesso de {usuario.nome} {'liberado' if usuario.ativo else 'bloqueado'}.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/<int:id_>/senha", methods=["POST"])
@login_required
@somente_admin
def trocar_senha(id_):
    usuario = buscar_ou_404(Usuario, id_)
    senha = request.form.get("senha", "")
    if len(senha) < 8:
        flash("A senha precisa ter ao menos 8 caracteres.", "erro")
    else:
        usuario.definir_senha(senha)
        db.session.commit()
        flash(f"Senha de {usuario.nome} alterada.", "ok")
    return redirect(url_for("usuarios"))


PAPEIS = {
    Papel.ADMIN: "Administrador",
    Papel.OPERADOR: "Operador",
    Papel.LEITURA: "Somente leitura",
}


def _historico_do_usuario(usuario):
    return {
        "notas lançadas": da_empresa(NotaFiscal).filter_by(
            criada_por_id=usuario.id).count(),
        "notas canceladas": da_empresa(NotaFiscal).filter_by(
            cancelada_por_id=usuario.id).count(),
        "pagamentos registrados": da_empresa(Titulo).filter_by(
            baixado_por_id=usuario.id).count(),
        "solicitações abertas": da_empresa(SolicitacaoCancelamento).filter_by(
            solicitante_id=usuario.id).count(),
        "solicitações respondidas": da_empresa(SolicitacaoCancelamento).filter_by(
            decidida_por_id=usuario.id).count(),
    }


@app.route("/usuarios/<int:id_>/excluir", methods=["POST"])
@login_required
@somente_admin
def excluir_usuario(id_):
    usuario = buscar_ou_404(Usuario, id_)

    if usuario.id == current_user.id:
        flash("Você não pode excluir o próprio cadastro.", "erro")
        return redirect(url_for("usuarios"))

    historico = {k: v for k, v in _historico_do_usuario(usuario).items() if v}
    if historico:
        resumo = ", ".join(f"{v} {k}" for k, v in historico.items())
        flash(
            f"{usuario.nome} não pode ser excluído porque tem histórico no sistema "
            f"({resumo}). Bloqueie o acesso para impedir a entrada sem apagar os "
            f"registros.", "erro")
        return redirect(url_for("usuarios"))

    nome = usuario.nome
    db.session.delete(usuario)
    db.session.commit()
    flash(f"Cadastro de {nome} excluído.", "ok")
    return redirect(url_for("usuarios"))


def _consulta_relatorio():
    consulta = da_empresa(Titulo).join(NotaFiscal).join(Fornecedor)

    de = request.args.get("de")
    ate = request.args.get("ate")
    base = request.args.get("base", "vencimento")
    campo = Titulo.data_pagamento if base == "pagamento" else Titulo.vencimento

    if de:
        consulta = consulta.filter(campo >= _data(de))
    if ate:
        consulta = consulta.filter(campo <= _data(ate))

    fornecedor_id = request.args.get("fornecedor_id", type=int)
    if fornecedor_id:
        consulta = consulta.filter(NotaFiscal.fornecedor_id == fornecedor_id)

    projeto = request.args.get("projeto")
    if projeto:
        consulta = consulta.filter(NotaFiscal.projeto == projeto)

    situacao = request.args.get("situacao")
    if situacao == StatusTitulo.CANCELADO:
        consulta = consulta.filter(
            db.or_(Titulo.status == StatusTitulo.CANCELADO,
                   NotaFiscal.status == StatusNota.CANCELADA)
        )
    else:
        consulta = consulta.filter(
            Titulo.status != StatusTitulo.CANCELADO,
            NotaFiscal.status != StatusNota.CANCELADA,
        )
        if situacao in (StatusTitulo.ABERTO, StatusTitulo.PAGO):
            consulta = consulta.filter(Titulo.status == situacao)

    return consulta.order_by(campo)


@app.route("/relatorios")
@login_required
def relatorios():
    linhas = _consulta_relatorio().all()

    total = sum((t.valor for t in linhas), Decimal("0.00"))
    pago = sum((t.valor_pago for t in linhas if t.valor_pago), Decimal("0.00"))
    aberto = sum((t.valor for t in linhas if t.status == StatusTitulo.ABERTO),
                 Decimal("0.00"))


    acrescimos = sum((t.acrescimo for t in linhas if t.valor_pago), Decimal("0.00"))

    por_fornecedor = {}
    for t in linhas:
        nome = t.nota.fornecedor.razao_social
        por_fornecedor[nome] = por_fornecedor.get(nome, Decimal("0.00")) + t.valor

    return render_template(
        "relatorios.html",
        linhas=linhas,
        total=total, pago=pago, aberto=aberto, acrescimos=acrescimos,
        por_fornecedor=sorted(por_fornecedor.items(), key=lambda x: -x[1]),
        fornecedores=da_empresa(Fornecedor).order_by(Fornecedor.razao_social).all(),
        projetos=_projetos_ativos(),
        hoje=date.today(),
    )


@app.route("/relatorios/csv")
@login_required
def relatorio_csv():
    saida = io.StringIO()
    escritor = csv.writer(saida, delimiter=";")
    escritor.writerow([
        "Vencimento", "Situacao", "Fornecedor", "CNPJ", "Nota", "Parcela",
        "Projeto", "Valor", "Data pagamento", "Valor pago",
        "Juros", "Multa", "Desconto",
    ])

    def br(valor):
        return f"{valor:.2f}".replace(".", ",") if valor is not None else ""

    for t in _consulta_relatorio().all():
        escritor.writerow([
            t.vencimento.strftime("%d/%m/%Y"),
            t.status,
            t.nota.fornecedor.razao_social,
            t.nota.fornecedor.cnpj,
            t.nota.numero,
            f"{t.numero_parcela}/{t.total_parcelas}",
            t.nota.projeto or "",
            br(t.valor),
            t.data_pagamento.strftime("%d/%m/%Y") if t.data_pagamento else "",
            br(t.valor_pago),
            br(t.juros), br(t.multa), br(t.desconto),
        ])

    nome = f"contas-a-pagar-{date.today():%Y-%m-%d}.csv"
    return Response(
        "\ufeff" + saida.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={nome}"},
    )


@app.route("/fornecedores/<int:id_>/editar", methods=["GET", "POST"])
@login_required
def editar_fornecedor(id_):
    fornecedor = buscar_ou_404(Fornecedor, id_)

    if request.method == "POST":
        cnpj = _so_digitos(request.form.get("cnpj"))
        outro = da_empresa(Fornecedor).filter(
            Fornecedor.cnpj == cnpj, Fornecedor.id != fornecedor.id).first()
        if outro:
            flash("Outro fornecedor já usa este CNPJ.", "erro")
        else:
            fornecedor.razao_social = request.form["razao_social"].strip()
            fornecedor.nome_fantasia = request.form.get("nome_fantasia", "").strip() or None
            fornecedor.cnpj = cnpj
            fornecedor.contato = request.form.get("contato", "").strip() or None
            fornecedor.email = request.form.get("email", "").strip() or None
            fornecedor.telefone = request.form.get("telefone", "").strip() or None
            db.session.commit()
            flash("Fornecedor atualizado.", "ok")
            return redirect(url_for("fornecedores"))

    return render_template("fornecedor_editar.html", fornecedor=fornecedor)


@app.route("/fornecedores/<int:id_>/alternar", methods=["POST"])
@login_required
def alternar_fornecedor(id_):
    fornecedor = buscar_ou_404(Fornecedor, id_)
    fornecedor.ativo = not fornecedor.ativo
    db.session.commit()
    flash(f"Fornecedor {'reativado' if fornecedor.ativo else 'desativado'}.", "ok")
    return redirect(url_for("fornecedores"))


@app.context_processor
def contador_solicitacoes():
    if not current_user.is_authenticated or current_user.papel != Papel.ADMIN:
        return {"solicitacoes_pendentes": 0}
    total = da_empresa(SolicitacaoCancelamento).filter_by(
        status=StatusSolicitacao.PENDENTE).count()
    return {"solicitacoes_pendentes": total}


@app.route("/notas/<int:id_>/solicitar-cancelamento", methods=["POST"])
@login_required
def solicitar_cancelamento(id_):
    nota = buscar_ou_404(NotaFiscal, id_)
    motivo = request.form.get("motivo", "").strip()

    if nota.status == StatusNota.CANCELADA:
        flash("Esta nota já está cancelada.", "erro")
    elif len(motivo) < 10:
        flash("Descreva a justificativa com pelo menos 10 caracteres.", "erro")
    elif nota.solicitacao_pendente:
        flash("Já existe uma solicitação pendente para esta nota.", "erro")
    else:
        db.session.add(SolicitacaoCancelamento(
            empresa_id=current_user.empresa_id,
            nota_fiscal_id=nota.id,
            solicitante_id=current_user.id,
            motivo=motivo,
        ))
        db.session.commit()
        flash("Solicitação enviada ao administrador.", "ok")

    return redirect(url_for("editar_nota", id_=nota.id))


@app.route("/solicitacoes")
@login_required
def solicitacoes():
    consulta = da_empresa(SolicitacaoCancelamento)
    if current_user.papel != Papel.ADMIN:
        consulta = consulta.filter_by(solicitante_id=current_user.id)

    filtro = request.args.get("filtro", "pendentes")
    if filtro == "pendentes":
        consulta = consulta.filter_by(status=StatusSolicitacao.PENDENTE)
    elif filtro in (StatusSolicitacao.APROVADA, StatusSolicitacao.RECUSADA):
        consulta = consulta.filter_by(status=filtro)

    itens, pagina = paginar(consulta.order_by(SolicitacaoCancelamento.criada_em.desc()))
    return render_template("solicitacoes.html", solicitacoes=itens,
                           filtro=filtro, filtros=FILTROS_SOLICITACAO, pagina=pagina)


@app.route("/solicitacoes/<int:id_>/decidir", methods=["POST"])
@login_required
@somente_admin
def decidir_solicitacao(id_):
    solicitacao = buscar_ou_404(SolicitacaoCancelamento, id_)
    resposta = request.form.get("resposta", "").strip()
    decisao = request.form.get("decisao")

    try:
        if decisao == "aprovar":
            solicitacao.aprovar(current_user, resposta or None)
            db.session.commit()
            flash(f"Nota {solicitacao.nota.numero} cancelada.", "ok")
        elif decisao == "recusar":
            if not resposta:
                flash("Informe o motivo da recusa.", "erro")
                return redirect(url_for("solicitacoes"))
            solicitacao.recusar(current_user, resposta)
            db.session.commit()
            flash("Solicitação recusada.", "ok")
        else:
            flash("Decisão inválida.", "erro")
    except ValueError as e:
        flash(str(e), "erro")

    return redirect(url_for("solicitacoes"))


FILTROS_SOLICITACAO = {
    "pendentes": "Pendentes",
    "aprovada": "Aprovadas",
    "recusada": "Recusadas",
    "todas": "Todas",
}


@app.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    if request.method == "POST":
        acao = request.form.get("acao")

        if acao == "dados":
            nome = request.form.get("nome", "").strip()
            email = request.form.get("email", "").strip().lower()
            ocupado = Usuario.query.filter(
                Usuario.email == email, Usuario.id != current_user.id).first()

            if not nome:
                flash("Informe seu nome.", "erro")
            elif ocupado:
                flash("Este e-mail já está em uso por outro usuário.", "erro")
            else:
                current_user.nome = nome
                current_user.email = email
                db.session.commit()
                flash("Dados atualizados.", "ok")

        elif acao == "senha":
            atual = request.form.get("senha_atual", "")
            nova = request.form.get("nova_senha", "")
            confirmacao = request.form.get("confirmacao", "")

            if not current_user.conferir_senha(atual):
                flash("A senha atual não confere.", "erro")
            elif len(nova) < 8:
                flash("A nova senha precisa ter ao menos 8 caracteres.", "erro")
            elif nova != confirmacao:
                flash("A confirmação não corresponde à nova senha.", "erro")
            elif nova == atual:
                flash("A nova senha precisa ser diferente da atual.", "erro")
            else:
                current_user.definir_senha(nova)
                db.session.commit()
                flash("Senha alterada.", "ok")

        return redirect(url_for("perfil"))

    lancadas = da_empresa(NotaFiscal).filter_by(criada_por_id=current_user.id).count()
    baixadas = da_empresa(Titulo).filter_by(baixado_por_id=current_user.id).count()

    return render_template("perfil.html", papeis=PAPEIS,
                           lancadas=lancadas, baixadas=baixadas)


def _data(valor):
    if not valor:
        return None
    if "/" in valor:
        d, m, a = valor.split("/")
        return date(int(a), int(m), int(d))
    a, m, d = valor.split("-")
    return date(int(a), int(m), int(d))


def _decimal(valor):
    if valor in (None, ""):
        return Decimal("0.00")
    texto = str(valor).strip().replace("R$", "").strip()
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    return Decimal(texto)


def _so_digitos(valor):
    return "".join(c for c in (valor or "") if c.isdigit())


def _moeda(valor):
    if valor is None:
        return "—"
    inteiro, _, centavos = f"{Decimal(valor):.2f}".partition(".")
    negativo = inteiro.startswith("-")
    inteiro = inteiro.lstrip("-")
    grupos = []
    while len(inteiro) > 3:
        grupos.insert(0, inteiro[-3:])
        inteiro = inteiro[:-3]
    grupos.insert(0, inteiro)
    return f"{'-' if negativo else ''}R$ {'.'.join(grupos)},{centavos}"


app.jinja_env.filters["moeda"] = _moeda
app.jinja_env.filters["data_br"] = lambda d: d.strftime("%d/%m/%Y") if d else "—"


if __name__ == "__main__":
    app.run(debug=True)