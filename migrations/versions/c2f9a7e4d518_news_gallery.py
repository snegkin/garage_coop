"""Галерея картинок для новостей

Revision ID: c2f9a7e4d518
Revises: b8e1d4c7a253
Create Date: 2026-11-13 12:00:00

Часто используемые картинки (субботник, собрание, тарифы…) — вставляются
в новость копией (app/news_gallery.py).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2f9a7e4d518'
down_revision: Union[str, Sequence[str], None] = 'b8e1d4c7a253'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'news_gallery_image',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=120), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('stored_filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=True),
        sa.Column('use_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('author_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['author_id'], ['user.id'], name=op.f('fk_news_gallery_image_author_id_user'),
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_news_gallery_image')),
    )


def downgrade() -> None:
    op.drop_table('news_gallery_image')
