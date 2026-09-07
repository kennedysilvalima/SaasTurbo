import sys

from app import app
from models import Empresa, Papel, Usuario, db


def instalar(razao_social, cnpj, nome, email, senha):
    with app.app_context():
        db.create_all()

        if Usuario.query.filter_by(email=email).first():
            print(f"Usuario {email} ja existe. Nada foi alterado.")
            return

        empresa = Empresa.query.filter_by(cnpj=cnpj).first()
        if empresa is None:
            empresa = Empresa(razao_social=razao_social, cnpj=cnpj)
            db.session.add(empresa)
            db.session.flush()

        usuario = Usuario(
            empresa_id=empresa.id,
            nome=nome,
            email=email.lower(),
            papel=Papel.ADMIN,
        )
        usuario.definir_senha(senha)
        db.session.add(usuario)
        db.session.commit()

        print(f"Empresa '{empresa.razao_social}' criada.")
        print(f"Usuario administrador: {usuario.email}")


if __name__ == "__main__":
    if len(sys.argv) == 6:
        instalar(*sys.argv[1:])
    else:
        print("Uso: python criar_banco.py \"Razao Social\" CNPJ \"Nome\" email senha")
        sys.exit(1)