import os
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from flask import (Flask, abort, flash, redirect, render_template, request,
                   url_for)
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from sqlalchemy import func

from models import (Empresa, Fornecedor, NotaFiscal, Papel, StatusNota,
                    StatusTitulo, Titulo, Usuario, db)

BASE = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "troque-isto-em-producao")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///" + os.path.join(BASE, "contas.sqlite3")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Entre para continuar."


@login_manager.user_loader
def carregar_usuario(user_id):
    return db.session.get(Usuario, int(user_id))


# ---------------------------------------------------------------------------
# Isolamento entre empresas
# ---------------------------------------------------------------------------

def da_empresa(modelo):
    """Ponto unico de filtro por empresa. Toda consulta comeca por aqui."""
    return modelo.query.filter_by(empresa_id=current_user.empresa_id)


def buscar_ou_404(modelo, id_):
    obj = da_empresa(modelo).filter_by(id=id_).first()
    if obj is None:
        abort(404)
    return obj


# ---------------------------------------------------------------------------
# Autenticacao
# ---------------------------------------------------------------------------

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

        # Mensagem generica: nao revela se o e-mail existe.
        flash("E-mail ou senha incorretos.", "erro")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Painel
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def painel():
    hoje = date.today()
    em_aberto = da_empresa(Titulo).filter_by(status=StatusTitulo.ABERTO)

    def somar(consulta):
        return consulta.with_entities(
            func.coalesce(func.sum(Titulo.valor), 0)
        ).scalar() or Decimal("0.00")

    vencidos = em_aberto.filter(Titulo.vencimento < hoje)
    vence_hoje = em_aberto.filter(Titulo.vencimento == hoje)
    proximos = em_aberto.filter(
        Titulo.vencimento > hoje, Titulo.vencimento <= hoje + timedelta(days=7)
    )

    pago_mes = da_empresa(Titulo).filter(
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


# ---------------------------------------------------------------------------
# Titulos
# ---------------------------------------------------------------------------

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
    hoje = date.today()
    consulta = da_empresa(Titulo)

    if filtro == "abertos":
        consulta = consulta.filter_by(status=StatusTitulo.ABERTO)
    elif filtro == "vencidos":
        consulta = consulta.filter(Titulo.status == StatusTitulo.ABERTO,
                                   Titulo.vencimento < hoje)
    elif filtro == "semana":
        consulta = consulta.filter(Titulo.status == StatusTitulo.ABERTO,
                                   Titulo.vencimento <= hoje + timedelta(days=7))
    elif filtro == "pagos":
        consulta = consulta.filter_by(status=StatusTitulo.PAGO)

    ordem = Titulo.data_pagamento.desc() if filtro == "pagos" else Titulo.vencimento
    return render_template(
        "titulos.html",
        titulos=consulta.order_by(ordem).all(),
        filtro=filtro,
        filtros=FILTROS,
        hoje=hoje,
    )


@app.route("/titulos/<int:id_>/baixar", methods=["POST"])
@login_required
def baixar_titulo(id_):
    titulo = buscar_ou_404(Titulo, id_)
    try:
        # Converte aqui, na borda: o formulario manda "1.234,56" e o model
        # trabalha so com Decimal.
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


# ---------------------------------------------------------------------------
# Notas
# ---------------------------------------------------------------------------

@app.route("/notas")
@login_required
def notas():
    lista = da_empresa(NotaFiscal).order_by(NotaFiscal.data_emissao.desc()).all()
    return render_template("notas.html", notas=lista)


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
                                   dados=request.form)

        nota = NotaFiscal(
            empresa_id=current_user.empresa_id,
            fornecedor_id=int(request.form["fornecedor_id"]),
            numero=request.form["numero"].strip(),
            serie=request.form.get("serie", "").strip() or None,
            chave_acesso=chave or None,
            data_emissao=_data(request.form["data_emissao"]),
            valor_total=_decimal(request.form["valor_total"]),
            tipo=request.form.get("tipo"),
            centro_custo=request.form.get("centro_custo", "").strip() or None,
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

    return render_template("nota_form.html", fornecedores=fornecedores, dados={})


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
    """Divide o valor e joga a diferenca de centavos na ultima parcela.

    R$ 100 em 3x vira 33,33 + 33,33 + 33,34. A soma sempre fecha com o total.
    """
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


# ---------------------------------------------------------------------------
# Fornecedores
# ---------------------------------------------------------------------------

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

    lista = da_empresa(Fornecedor).order_by(Fornecedor.razao_social).all()
    return render_template("fornecedores.html", fornecedores=lista)


# ---------------------------------------------------------------------------
# Conversao e formatacao
# ---------------------------------------------------------------------------

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