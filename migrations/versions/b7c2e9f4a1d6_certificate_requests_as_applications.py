"""Запрос справки о владении — обычное заявление в правление

Revision ID: b7c2e9f4a1d6
Revises: a4d9e2b7c315
Create Date: 2026-11-16 20:00:00

Отдельный запрос справки из кабинета (certificate_request) заменён видом
заявления CERTIFICATE (app/applications.py): application.document_id —
оформленная по заявлению справка (IssuedDocument). Старые запросы
переносятся в заявления (номер — из счётчика заявлений по году запроса,
реквизиты заявителя — из карточки человека), таблица удаляется.
"""
import datetime as dt
from html import escape
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c2e9f4a1d6'
down_revision: Union[str, Sequence[str], None] = 'a4d9e2b7c315'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS = {"REQUESTED": "SUBMITTED", "READY": "ANSWERED", "HANDED_OVER": "ANSWERED", "REJECTED": "REJECTED"}


def _as_datetime(value):
    if value is None or isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(str(value))


def _next_number(conn, year: int) -> str:
    row = conn.execute(sa.text("SELECT last_number FROM application_counter WHERE year = :y"), {"y": year}).first()
    if row is None:
        conn.execute(sa.text("INSERT INTO application_counter (year, last_number) VALUES (:y, 1)"), {"y": year})
        return f"{year}-1"
    conn.execute(sa.text("UPDATE application_counter SET last_number = :n WHERE year = :y"), {"n": row[0] + 1, "y": year})
    return f"{year}-{row[0] + 1}"


def upgrade() -> None:
    with op.batch_alter_table("application") as batch:
        batch.add_column(sa.Column("document_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            batch.f("fk_application_document_id_issued_document"), "issued_document", ["document_id"], ["id"],
            ondelete="SET NULL",
        )

    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT r.person_id, r.garage_id, r.garage_number, r.comment, r.status, r.created_at, r.document_id, "
        "r.ready_at, r.reject_reason, p.full_name, p.registration_address "
        "FROM certificate_request r JOIN person p ON p.id = r.person_id ORDER BY r.created_at"
    )).fetchall()
    for (person_id, garage_id, garage_number, comment, status, created_at, document_id,
         ready_at, reject_reason, full_name, address) in rows:
        created_at = _as_datetime(created_at)
        body = f"<p>Прошу выдать справку о владении гаражом № {escape(garage_number)}.</p>"
        if comment:
            body += f"<p>{escape(comment)}</p>"
        new_status = _STATUS.get(status, "SUBMITTED")
        response = None
        if new_status == "ANSWERED":
            response = "<p>Справка о владении гаражом подготовлена — заберите подписанный экземпляр у председателя.</p>"
        elif reject_reason:
            response = f"<p>{escape(reject_reason)}</p>"
        conn.execute(sa.text(
            "INSERT INTO application (number, kind, title, body_html, status, person_id, garage_id, created_at, "
            "applicant_name, applicant_address, garage_number, response_html, decided_at, document_id) "
            "VALUES (:number, 'CERTIFICATE', 'о выдаче справки о владении гаражом', :body, :status, :person_id, "
            ":garage_id, :created_at, :name, :address, :garage_number, :response, :decided_at, :document_id)"
        ), {
            "number": _next_number(conn, created_at.year), "body": body, "status": new_status,
            "person_id": person_id, "garage_id": garage_id, "created_at": created_at, "name": full_name,
            "address": address, "garage_number": garage_number, "response": response,
            "decided_at": _as_datetime(ready_at) or (created_at if new_status != "SUBMITTED" else None),
            "document_id": document_id,
        })

    op.drop_table("certificate_request")


def downgrade() -> None:
    op.create_table(
        "certificate_request",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("garage_id", sa.Integer(), nullable=True),
        sa.Column("garage_number", sa.String(length=20), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("REQUESTED", "READY", "HANDED_OVER", "REJECTED", name="certificaterequeststatus"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("ready_at", sa.DateTime(), nullable=True),
        sa.Column("handed_over_at", sa.DateTime(), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["person_id"], ["person.id"], name=op.f("fk_certificate_request_person_id_person"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["garage_id"], ["garage.id"], name=op.f("fk_certificate_request_garage_id_garage"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["issued_document.id"], name=op.f("fk_certificate_request_document_id_issued_document"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_certificate_request")),
    )
    with op.batch_alter_table("certificate_request") as batch:
        batch.create_index(batch.f("ix_certificate_request_person_id"), ["person_id"], unique=False)
        batch.create_index(batch.f("ix_certificate_request_garage_id"), ["garage_id"], unique=False)
        batch.create_index(batch.f("ix_certificate_request_status"), ["status"], unique=False)
    op.execute("DELETE FROM application WHERE kind = 'CERTIFICATE'")
    with op.batch_alter_table("application") as batch:
        batch.drop_constraint(batch.f("fk_application_document_id_issued_document"), type_="foreignkey")
        batch.drop_column("document_id")
