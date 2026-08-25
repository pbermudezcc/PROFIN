"""
app/categorization/category_manager.py
========================================
PROFIN — Category Manager

Responsibility:
  CRUD operations for the category tree and seed data.

  Category hierarchy (2 levels used in v1):
    Level 1 — type       : GASTO | INGRESO | SERV. BANCARIO | INVERSION
                           Each type node carries a transaction_type field
                           that maps to TransactionType enum.
    Level 2 — category   : e.g. "Supermercados", "Nómina Pedro"
    Level 3 — subcategory: reserved for future use (not seeded in v1)

  Seed data:
    seed_default_categories(session) creates the full category tree from
    Pedro's Excel (Categorías.png) if the DB is empty.

  Rules alignment:
    AutoCategorizationRule.user_id is nullable — NULL means global rule
    (applies to all users). Per-user rules override global rules at the
    same priority level.

Design decisions:
  - No user_id required for reading categories in v1 (single-user app).
  - category_id is always the leaf node (category level) — types are
    structural only and never assigned directly to transactions.
  - get_all_categories() returns a flat list sorted by type → category
    for GUI display.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.db.models import (
    Category,
    CategoryLevel,
    TransactionType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seed data — from Pedro's Excel (Categorías.png)
# ---------------------------------------------------------------------------

_SEED: list[dict] = [
    # ── GASTO ────────────────────────────────────────────────────────────
    {
        "type_name":        "GASTO",
        "transaction_type": TransactionType.expense,
        "categories": [
            "Teléfono",
            "Luz",
            "Com. Propiertarios (Cuota)",
            "Com. Propiertarios (Gas & ACS)",
            "Agua Tasa Municipal",
            "Agua Consumo",
            "Asistenta Limpieza",
            "Supermercados",
            "Guardería",
            "Apple iCloud",
            "Spotify",
            "Netflix",
            "HBO",
            "Amazon Prime",
            "Restaurantes & Bares",
            "Compras Hogar",
            "IKEA",
            "Wallapop",
            "Multas",
            "Sklum",
            "Glovo",
            "Peajes",
            "Gasolina",
            "Regalo boda",
            "Amazon",
            "IBI",
            "Compra Vivienda",
            "Reforma Vivienda",
            "Hipoteca",
            "Recibo MyBox - Cuota Agrupada",
            "Gastos Pedro Jr",
            "Ocio, Eventos y Bodas",
            "Parking",
            "Tabaco",
        ],
    },
    # ── INGRESO ──────────────────────────────────────────────────────────
    {
        "type_name":        "INGRESO",
        "transaction_type": TransactionType.income,
        "categories": [
            "Nomina Maria",
            "Nomina Pedro",
            "Aporte Comun María",
            "Aporte Comun Pedro",
            "Prestacion Maternidad Maria",
            "Prestacion Paternidad Pedro",
            "Ayuda Cuidado Menor",
            "Compra Vivienda",
            "Regalos Boda",
            "Reforma Vivienda",
            "HBO",
        ],
    },
    # ── SERV. BANCARIO ───────────────────────────────────────────────────
    {
        "type_name":        "SERV. BANCARIO",
        "transaction_type": TransactionType.expense,
        "categories": [
            "Hipoteca",
        ],
    },
    # ── INVERSION ────────────────────────────────────────────────────────
    {
        "type_name":        "INVERSION",
        "transaction_type": TransactionType.investment,
        "categories": [
            "Tressis",
            "Aporte Broker",
        ],
    },
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def seed_default_categories(session: Session, user_id: int) -> int:
    """
    Seed the default category tree if no categories exist for this user.

    Parameters
    ----------
    session : Active SQLAlchemy session.
    user_id : User to seed categories for.

    Returns
    -------
    int : Number of category rows created (0 if already seeded).
    """
    existing = (
        session.query(Category)
        .filter(Category.user_id == user_id)
        .count()
    )
    if existing > 0:
        logger.info(
            "Categorías ya existentes para user_id=%d (%d filas). "
            "Seed omitido.", user_id, existing
        )
        return 0

    created = 0
    now = datetime.now(timezone.utc)

    for type_block in _SEED:
        # Create type node (level = type)
        type_node = Category(
            user_id          = user_id,
            parent_id        = None,
            name             = type_block["type_name"],
            level            = CategoryLevel.type,
            transaction_type = type_block["transaction_type"],
            created_at       = now,
        )
        session.add(type_node)
        session.flush()  # get type_node.id before inserting children
        created += 1

        # Create category nodes (level = category)
        for cat_name in type_block["categories"]:
            cat_node = Category(
                user_id          = user_id,
                parent_id        = type_node.id,
                name             = cat_name,
                level            = CategoryLevel.category,
                transaction_type = None,
                created_at       = now,
            )
            session.add(cat_node)
            created += 1

    session.commit()
    logger.info(
        "Seed completado para user_id=%d: %d categorías creadas.",
        user_id, created
    )
    return created


def get_all_categories(
    session: Session,
    user_id: int,
    level: Optional[CategoryLevel] = None,
) -> List[Category]:
    """
    Return all categories for a user, optionally filtered by level.
    Sorted by: type name → category name.

    Parameters
    ----------
    session : Active SQLAlchemy session.
    user_id : Target user.
    level   : Optional filter (CategoryLevel.type / .category / .subcategory).

    Returns
    -------
    List[Category] sorted alphabetically within each type.
    """
    query = session.query(Category).filter(Category.user_id == user_id)
    if level is not None:
        query = query.filter(Category.level == level)
    return query.order_by(Category.name).all()


def get_category_by_id(
    session: Session,
    category_id: int,
) -> Optional[Category]:
    """Return a Category by primary key, or None if not found."""
    return session.get(Category, category_id)


def create_category(
    session:          Session,
    user_id:          int,
    name:             str,
    level:            CategoryLevel,
    parent_id:        Optional[int]          = None,
    transaction_type: Optional[TransactionType] = None,
) -> Category:
    """
    Create a new category node.

    Raises
    ------
    ValueError : If name is empty, or parent_id is missing for non-type levels.
    """
    if not name or not name.strip():
        raise ValueError("El nombre de la categoría no puede estar vacío.")
    if level != CategoryLevel.type and parent_id is None:
        raise ValueError(
            f"Las categorías de nivel '{level.value}' requieren un parent_id."
        )

    cat = Category(
        user_id          = user_id,
        parent_id        = parent_id,
        name             = name.strip(),
        level            = level,
        transaction_type = transaction_type,
        created_at       = datetime.now(timezone.utc),
    )
    session.add(cat)
    session.commit()
    session.refresh(cat)
    logger.info(
        "Categoría creada: '%s' (level=%s, id=%d)",
        cat.name, cat.level.value, cat.id
    )
    return cat


def update_category_name(
    session:     Session,
    category_id: int,
    new_name:    str,
) -> Category:
    """
    Rename an existing category.

    Raises
    ------
    ValueError : If category not found or new name is empty.
    """
    if not new_name or not new_name.strip():
        raise ValueError("El nuevo nombre no puede estar vacío.")

    cat = session.get(Category, category_id)
    if cat is None:
        raise ValueError(f"Categoría con id={category_id} no encontrada.")

    cat.name = new_name.strip()
    session.commit()
    session.refresh(cat)
    logger.info("Categoría id=%d renombrada a '%s'.", category_id, cat.name)
    return cat


def delete_category(
    session:     Session,
    category_id: int,
) -> None:
    """
    Delete a category and all its children (cascade).
    Transactions assigned to this category will have category_id set to NULL
    (handled by the DB FK ondelete=SET NULL).

    Raises
    ------
    ValueError : If category not found.
    """
    cat = session.get(Category, category_id)
    if cat is None:
        raise ValueError(f"Categoría con id={category_id} no encontrada.")

    name = cat.name
    session.delete(cat)
    session.commit()
    logger.info("Categoría '%s' (id=%d) eliminada.", name, category_id)


def assign_transaction_category(
    session:        Session,
    transaction_id: int,
    category_id:    Optional[int],
) -> None:
    """
    Manually assign (or clear) the category of a transaction.

    Parameters
    ----------
    transaction_id : Target transaction.
    category_id    : New category ID, or None to clear.

    Raises
    ------
    ValueError : If transaction not found.
    """
    from app.db.models import Transaction  # local import to avoid circular

    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise ValueError(f"Transacción con id={transaction_id} no encontrada.")

    txn.category_id = category_id
    session.commit()
    logger.info(
        "Transacción id=%d → category_id=%s (asignación manual).",
        transaction_id, category_id
    )