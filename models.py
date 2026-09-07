from datetime import date, datetime
from decimal import Decimal

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class StatusNota:
    PENDENTE = "pendente"
    CANCELADA = "cancelada"


class StatusTitulo:
    ABERTO = "aberto"
    PAGO = "pago"
    CANCELADO = "cancelado"


class Papel:
    ADMIN = "admin"
    OPERADOR = "operador"
    LEITURA = "leitura"


class Empresa(db.Model):
    __tablename__ = "empresa"

    id = db.Column(db.Integer, primary_key=True)
    razao_social = db.Column(db.String(150), nullable=False)
    nome_fantasia = db.Column(db.String(150))
    cnpj = db.Column(db.String(14), unique=True, nullable=False)

    ativa = db.Column(db.Boolean, default=True, nullable=False)
    criada_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    usuarios = db.relationship("Usuario", back_populates="empresa")
    fornecedores = db.relationship("Fornecedor", back_populates="empresa")

    def __repr__(self):
        return f"<Empresa {self.id} {self.razao_social}>"


class Usuario(UserMixin, db.Model):
    __tablename__ = "usuario"
    __table_args__ = (
        db.UniqueConstraint("empresa_id", "email", name="uq_usuario_email_empresa"),
    )

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey("empresa.id"), nullable=False)

    nome = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    senha_hash = db.Column(db.String(255), nullable=False)
    papel = db.Column(db.String(20), default=Papel.OPERADOR, nullable=False)

    ativo = db.Column(db.Boolean, default=True, nullable=False)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    empresa = db.relationship("Empresa", back_populates="usuarios")

    def definir_senha(self, senha):
        self.senha_hash = generate_password_hash(senha)

    def conferir_senha(self, senha):
        return check_password_hash(self.senha_hash, senha)

    def __repr__(self):
        return f"<Usuario {self.id} {self.email}>"


class Fornecedor(db.Model):
    __tablename__ = "fornecedor"
    __table_args__ = (
        db.UniqueConstraint("empresa_id", "cnpj", name="uq_fornecedor_cnpj_empresa"),
    )

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey("empresa.id"), nullable=False)

    razao_social = db.Column(db.String(150), nullable=False)
    nome_fantasia = db.Column(db.String(150))
    cnpj = db.Column(db.String(14), nullable=False)

    contato = db.Column(db.String(120))
    email = db.Column(db.String(120))
    telefone = db.Column(db.String(20))

    ativo = db.Column(db.Boolean, default=True, nullable=False)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    empresa = db.relationship("Empresa", back_populates="fornecedores")
    notas = db.relationship("NotaFiscal", back_populates="fornecedor")

    def __repr__(self):
        return f"<Fornecedor {self.id} {self.razao_social}>"


class CentroCusto(db.Model):
    __tablename__ = "centro_custo"
    __table_args__ = (
        db.UniqueConstraint("empresa_id", "nome", name="uq_centro_nome_empresa"),
    )

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey("empresa.id"), nullable=False)

    nome = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.String(200))
    ativo = db.Column(db.Boolean, default=True, nullable=False)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<CentroCusto {self.id} {self.nome}>"


class NotaFiscal(db.Model):
    __tablename__ = "nota_fiscal"
    __table_args__ = (

        db.UniqueConstraint("empresa_id", "chave_acesso", name="uq_nf_chave_empresa"),

        db.UniqueConstraint(
            "empresa_id", "fornecedor_id", "numero", "serie",
            name="uq_nf_numero_empresa",
        ),
        db.Index("ix_nf_empresa_emissao", "empresa_id", "data_emissao"),
    )

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey("empresa.id"), nullable=False)
    fornecedor_id = db.Column(db.Integer, db.ForeignKey("fornecedor.id"), nullable=False)

    numero = db.Column(db.String(20), nullable=False)
    serie = db.Column(db.String(10))
    chave_acesso = db.Column(db.String(44))

    data_emissao = db.Column(db.Date, nullable=False)
    data_entrada = db.Column(db.Date, default=date.today)

    valor_total = db.Column(db.Numeric(14, 2), nullable=False)

    tipo = db.Column(db.String(20))
    centro_custo = db.Column(db.String(80))
    descricao = db.Column(db.Text)

    status = db.Column(db.String(20), default=StatusNota.PENDENTE, nullable=False)
    motivo_cancelamento = db.Column(db.Text)
    cancelada_em = db.Column(db.DateTime)
    cancelada_por_id = db.Column(db.Integer, db.ForeignKey("usuario.id"))

    arquivo_pdf = db.Column(db.String(255))
    arquivo_xml = db.Column(db.String(255))

    criada_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    criada_por_id = db.Column(db.Integer, db.ForeignKey("usuario.id"))

    fornecedor = db.relationship("Fornecedor", back_populates="notas")
    titulos = db.relationship(
        "Titulo", back_populates="nota", cascade="all, delete-orphan"
    )
    solicitacoes = db.relationship(
        "SolicitacaoCancelamento", back_populates="nota",
        cascade="all, delete-orphan", order_by="SolicitacaoCancelamento.criada_em.desc()"
    )


    @property
    def total_titulos(self):
        return sum(
            (t.valor for t in self.titulos if t.status != StatusTitulo.CANCELADO),
            Decimal("0.00"),
        )

    @property
    def diferenca(self):
        return (self.valor_total or Decimal("0.00")) - self.total_titulos

    @property
    def fecha(self):
        return abs(self.diferenca) <= Decimal("0.02")

    @property
    def sem_titulos(self):
        return not any(t.status != StatusTitulo.CANCELADO for t in self.titulos)

    @property
    def total_pago(self):
        return sum(
            (t.valor_pago for t in self.titulos if t.valor_pago),
            Decimal("0.00"),
        )


    @property
    def tem_pagamento(self):
        return any(t.status == StatusTitulo.PAGO for t in self.titulos)



    @property
    def solicitacao_pendente(self):
        for s in self.solicitacoes:
            if s.status == "pendente":
                return s
        return None

    def cancelar(self, usuario, motivo):
        self.status = StatusNota.CANCELADA
        self.motivo_cancelamento = motivo
        self.cancelada_em = datetime.utcnow()
        self.cancelada_por_id = usuario.id

        for titulo in self.titulos:
            if titulo.status == StatusTitulo.ABERTO:
                titulo.status = StatusTitulo.CANCELADO

    def __repr__(self):
        return f"<NotaFiscal {self.id} n{self.numero}>"


class Titulo(db.Model):
    __tablename__ = "titulo"
    __table_args__ = (
        db.Index("ix_titulo_empresa_venc", "empresa_id", "vencimento"),
        db.Index("ix_titulo_empresa_status", "empresa_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey("empresa.id"), nullable=False)
    nota_fiscal_id = db.Column(db.Integer, db.ForeignKey("nota_fiscal.id"), nullable=False)

    numero_parcela = db.Column(db.Integer, default=1, nullable=False)
    total_parcelas = db.Column(db.Integer, default=1, nullable=False)

    vencimento = db.Column(db.Date, nullable=False)
    valor = db.Column(db.Numeric(14, 2), nullable=False)

    linha_digitavel = db.Column(db.String(60))
    forma_pagamento = db.Column(db.String(30))

    data_pagamento = db.Column(db.Date)
    valor_pago = db.Column(db.Numeric(14, 2))
    juros = db.Column(db.Numeric(14, 2), default=Decimal("0.00"))
    multa = db.Column(db.Numeric(14, 2), default=Decimal("0.00"))
    desconto = db.Column(db.Numeric(14, 2), default=Decimal("0.00"))

    status = db.Column(db.String(20), default=StatusTitulo.ABERTO, nullable=False)
    observacao = db.Column(db.Text)

    criado_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    baixado_por_id = db.Column(db.Integer, db.ForeignKey("usuario.id"))

    nota = db.relationship("NotaFiscal", back_populates="titulos")


    @property
    def vencido(self):
        return self.status == StatusTitulo.ABERTO and self.vencimento < date.today()

    @property
    def dias_para_vencer(self):
        return (self.vencimento - date.today()).days

    @property
    def dias_atraso(self):
        return max(0, -self.dias_para_vencer) if self.status == StatusTitulo.ABERTO else 0

    @property
    def acrescimo(self):
        if self.valor_pago is None:
            return Decimal("0.00")
        return self.valor_pago - self.valor


    def baixar(self, usuario, data_pagamento, valor_pago,
               juros=None, multa=None, desconto=None, observacao=None):

        if self.status != StatusTitulo.ABERTO:
            raise ValueError("Somente titulos em aberto podem ser baixados.")

        self.data_pagamento = data_pagamento
        self.valor_pago = Decimal(str(valor_pago))
        self.juros = Decimal(str(juros or 0))
        self.multa = Decimal(str(multa or 0))
        self.desconto = Decimal(str(desconto or 0))
        self.status = StatusTitulo.PAGO
        self.baixado_por_id = usuario.id
        if observacao:
            self.observacao = observacao

    def estornar(self, usuario, motivo):
        if self.status != StatusTitulo.PAGO:
            raise ValueError("Somente titulos pagos podem ser estornados.")

        registro = f"[estorno {date.today():%d/%m/%Y} por {usuario.nome}] {motivo}"
        self.observacao = f"{self.observacao}\n{registro}" if self.observacao else registro

        self.data_pagamento = None
        self.valor_pago = None
        self.juros = Decimal("0.00")
        self.multa = Decimal("0.00")
        self.desconto = Decimal("0.00")
        self.status = StatusTitulo.ABERTO
        self.baixado_por_id = None

    def __repr__(self):
        return f"<Titulo {self.id} venc={self.vencimento} {self.status}>"


class StatusSolicitacao:
    PENDENTE = "pendente"
    APROVADA = "aprovada"
    RECUSADA = "recusada"


class SolicitacaoCancelamento(db.Model):
    __tablename__ = "solicitacao_cancelamento"

    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey("empresa.id"), nullable=False)
    nota_fiscal_id = db.Column(db.Integer, db.ForeignKey("nota_fiscal.id"), nullable=False)

    solicitante_id = db.Column(db.Integer, db.ForeignKey("usuario.id"), nullable=False)
    motivo = db.Column(db.Text, nullable=False)
    criada_em = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    status = db.Column(db.String(20), default=StatusSolicitacao.PENDENTE, nullable=False)
    decidida_por_id = db.Column(db.Integer, db.ForeignKey("usuario.id"))
    decidida_em = db.Column(db.DateTime)
    resposta = db.Column(db.Text)

    nota = db.relationship("NotaFiscal", back_populates="solicitacoes")
    solicitante = db.relationship("Usuario", foreign_keys=[solicitante_id])
    decidida_por = db.relationship("Usuario", foreign_keys=[decidida_por_id])

    @property
    def pendente(self):
        return self.status == StatusSolicitacao.PENDENTE

    def aprovar(self, usuario, resposta=None):
        if not self.pendente:
            raise ValueError("Esta solicitação já foi respondida.")
        self.status = StatusSolicitacao.APROVADA
        self.decidida_por_id = usuario.id
        self.decidida_em = datetime.utcnow()
        self.resposta = resposta
        self.nota.cancelar(usuario, f"{self.motivo} (solicitado por {self.solicitante.nome})")

    def recusar(self, usuario, resposta):
        if not self.pendente:
            raise ValueError("Esta solicitação já foi respondida.")
        self.status = StatusSolicitacao.RECUSADA
        self.decidida_por_id = usuario.id
        self.decidida_em = datetime.utcnow()
        self.resposta = resposta

    def __repr__(self):
        return f"<SolicitacaoCancelamento {self.id} {self.status}>"