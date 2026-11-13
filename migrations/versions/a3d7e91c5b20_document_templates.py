"""Бланки и шаблоны документов; справки о владении — в общий журнал; телефон и соцсети кооператива

Revision ID: a3d7e91c5b20
Revises: f7c2d9a4e186
Create Date: 2026-11-13 21:00:00

document_template, issued_document, document_counter (app/documents.py);
cooperative.phone, cooperative.social_links — для шапки бланка и
подстановок. Стандартные шаблоны не вставляются миграцией — их добавляет
кнопка на странице раздела (documents.add_default_templates); шаблон
справки о владении создаётся и при первом обращении к нему.

Справки о владении гаражом больше не отдельная сущность: garage_certificate
и certificate_counter удаляются, выданные справки (если были) переносятся
в issued_document с теми же номерами, certificate_request.certificate_id
→ document_id. Счётчик документов продолжает счётчик справок.
"""
from typing import Sequence, Union

import datetime as dt

from alembic import op
import sqlalchemy as sa


revision: str = 'a3d7e91c5b20'
down_revision: Union[str, Sequence[str], None] = 'f7c2d9a4e186'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'document_template',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('addressee_html', sa.Text(), nullable=False),
        sa.Column('body_html', sa.Text(), nullable=False),
        sa.Column('recipient', sa.String(length=10), nullable=False),
        sa.Column('show_number', sa.Boolean(), nullable=False),
        sa.Column('show_signature', sa.Boolean(), nullable=False),
        sa.Column('signer_role', sa.String(length=100), nullable=False),
        sa.Column('code', sa.String(length=40), nullable=True),
        sa.Column('chairman_only', sa.Boolean(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('is_archived', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_document_template')),
        sa.UniqueConstraint('code', name=op.f('uq_document_template_code')),
    )
    op.create_table(
        'issued_document',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('template_id', sa.Integer(), nullable=True),
        sa.Column('number', sa.String(length=30), nullable=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('addressee_html', sa.Text(), nullable=False),
        sa.Column('body_html', sa.Text(), nullable=False),
        sa.Column('issued_at', sa.Date(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=True),
        sa.Column('garage_id', sa.Integer(), nullable=True),
        sa.Column('show_signature', sa.Boolean(), nullable=False),
        sa.Column('signer_role', sa.String(length=100), nullable=False),
        sa.Column('signer_person_id', sa.Integer(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['template_id'], ['document_template.id'],
                                name=op.f('fk_issued_document_template_id_document_template'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['person_id'], ['person.id'],
                                name=op.f('fk_issued_document_person_id_person'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['garage_id'], ['garage.id'],
                                name=op.f('fk_issued_document_garage_id_garage'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['signer_person_id'], ['person.id'],
                                name=op.f('fk_issued_document_signer_person_id_person'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['user.id'],
                                name=op.f('fk_issued_document_created_by_user_id_user'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_issued_document')),
        sa.UniqueConstraint('number', name=op.f('uq_issued_document_number')),
    )
    with op.batch_alter_table('issued_document') as batch_op:
        batch_op.create_index(batch_op.f('ix_issued_document_template_id'), ['template_id'])
        batch_op.create_index(batch_op.f('ix_issued_document_issued_at'), ['issued_at'])
        batch_op.create_index(batch_op.f('ix_issued_document_person_id'), ['person_id'])
        batch_op.create_index(batch_op.f('ix_issued_document_garage_id'), ['garage_id'])
    op.create_table(
        'document_counter',
        sa.Column('year', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('last_number', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('year', name=op.f('pk_document_counter')),
    )
    with op.batch_alter_table('cooperative') as batch_op:
        batch_op.add_column(sa.Column('phone', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('social_links', sa.Text(), nullable=True))

    with op.batch_alter_table('certificate_request') as batch_op:
        batch_op.add_column(sa.Column('document_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_certificate_request_document_id_issued_document'),
                                    'issued_document', ['document_id'], ['id'], ondelete='SET NULL')
    _move_certificates()
    with op.batch_alter_table('certificate_request') as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_certificate_request_certificate_id_garage_certificate'),
                                 type_='foreignkey')
        batch_op.drop_column('certificate_id')
    op.drop_table('garage_certificate')
    op.drop_table('certificate_counter')


def _move_certificates() -> None:
    conn = op.get_bind()
    certificates = conn.execute(sa.text(
        "SELECT id, number, person_id, garage_id, body_html, issued_at, issued_by_person_id, created_at "
        "FROM garage_certificate ORDER BY id"
    )).mappings().all()
    for c in certificates:
        result = conn.execute(sa.text(
            "INSERT INTO issued_document (template_id, number, title, addressee_html, body_html, issued_at, "
            "person_id, garage_id, show_signature, signer_role, signer_person_id, created_by_user_id, created_at) "
            "VALUES (NULL, :number, 'СПРАВКА', '<p>по месту требования</p>', :body_html, :issued_at, "
            ":person_id, :garage_id, 1, 'Председатель', :issued_by_person_id, NULL, :created_at)"
        ), {**c, "created_at": c["created_at"] or dt.datetime.now()})
        conn.execute(sa.text("UPDATE certificate_request SET document_id = :doc WHERE certificate_id = :cert"),
                     {"doc": result.lastrowid, "cert": c["id"]})
    for row in conn.execute(sa.text("SELECT year, last_number FROM certificate_counter")).mappings().all():
        conn.execute(sa.text("INSERT INTO document_counter (year, last_number) VALUES (:year, :last_number)"), row)


def downgrade() -> None:
    # Справки обратно не разносятся — таблицы восстанавливаются пустыми.
    op.create_table(
        'certificate_counter',
        sa.Column('year', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('last_number', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('year', name=op.f('pk_certificate_counter')),
    )
    op.create_table(
        'garage_certificate',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('number', sa.String(length=30), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('garage_id', sa.Integer(), nullable=True),
        sa.Column('garage_number', sa.String(length=20), nullable=False),
        sa.Column('body_html', sa.Text(), nullable=False),
        sa.Column('issued_at', sa.Date(), nullable=False),
        sa.Column('issued_by_person_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['garage_id'], ['garage.id'], name=op.f('fk_garage_certificate_garage_id_garage'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['issued_by_person_id'], ['person.id'], name=op.f('fk_garage_certificate_issued_by_person_id_person'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['person_id'], ['person.id'], name=op.f('fk_garage_certificate_person_id_person'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_garage_certificate')),
        sa.UniqueConstraint('number', name=op.f('uq_garage_certificate_number')),
    )
    with op.batch_alter_table('certificate_request') as batch_op:
        batch_op.add_column(sa.Column('certificate_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_certificate_request_certificate_id_garage_certificate'),
                                    'garage_certificate', ['certificate_id'], ['id'], ondelete='SET NULL')
        batch_op.drop_constraint(batch_op.f('fk_certificate_request_document_id_issued_document'), type_='foreignkey')
        batch_op.drop_column('document_id')
    with op.batch_alter_table('cooperative') as batch_op:
        batch_op.drop_column('social_links')
        batch_op.drop_column('phone')
    op.drop_table('document_counter')
    with op.batch_alter_table('issued_document') as batch_op:
        batch_op.drop_index(batch_op.f('ix_issued_document_garage_id'))
        batch_op.drop_index(batch_op.f('ix_issued_document_person_id'))
        batch_op.drop_index(batch_op.f('ix_issued_document_issued_at'))
        batch_op.drop_index(batch_op.f('ix_issued_document_template_id'))
    op.drop_table('issued_document')
    op.drop_table('document_template')
