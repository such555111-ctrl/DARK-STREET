import os
import json
import uuid
import logging
import threading
import functools
import re
import io
import math
from datetime import datetime, timezone

from flask import Flask, request, send_from_directory, abort, jsonify, Response
import telebot
from telebot import types

from sqlalchemy import (create_engine, Column, Integer, BigInteger, String, Text,
                        Boolean, DateTime, CheckConstraint, LargeBinary, Float, JSON, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hqm_shop")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

# Render автоматически прокидывает RENDER_EXTERNAL_URL для web-сервисов.
# Можно переопределить вручную через WEBHOOK_URL, если нужно.
BASE_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Telegram-аккаунт менеджера (без @) — на него ведёт кнопка «Написать менеджеру».
SHOP_CONTACT = (os.environ.get("SHOP_CONTACT") or "HQManager1").lstrip("@").strip()

# Пароль для входа в админ-панель (/admin). Задаётся в переменных окружения.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# Telegram ID администратора(ов). Только этим пользователям бот присылает
# ссылку на админ-панель по команде /start. Можно указать несколько ID
# через запятую: "111111,222222".
ADMIN_CHAT_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_CHAT_ID", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(APP_DIR, "products.json")
ORDERS_FILE = os.path.join(APP_DIR, "orders.json")
QUESTIONS_FILE = os.path.join(APP_DIR, "questions.json")
PROMOS_FILE = os.path.join(APP_DIR, "promos.json")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")   # только для разового переноса старых фото в базу

# Доставка и самовывоз
try:
    DELIVERY_FEE = int(os.environ.get("DELIVERY_FEE", "59"))
except ValueError:
    DELIVERY_FEE = 59
# Бесплатная доставка от этой суммы товаров (после промокода, до списания баллов). 0 — отключить.
try:
    FREE_DELIVERY_FROM = int(os.environ.get("FREE_DELIVERY_FROM", "990"))
except ValueError:
    FREE_DELIVERY_FROM = 990


def delivery_fee_for(goods_total):
    """Стоимость доставки: 0 ₽ от FREE_DELIVERY_FROM, иначе DELIVERY_FEE."""
    if FREE_DELIVERY_FROM > 0 and goods_total >= FREE_DELIVERY_FROM:
        return 0
    return DELIVERY_FEE


PICKUP_POINTS = [
    p.strip() for p in (os.environ.get("PICKUP_POINTS") or "Казань (личная доставка)").split(",") if p.strip()
]

# Программа лояльности: 1 балл = 1 рубль
def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)

POINT_VALUE = _env_float("POINT_VALUE_RUB", 0.5)             # 1 балл = 0,5 ₽
CASHBACK_PERCENT = _env_float("CASHBACK_PERCENT", 5)        # % кешбэка от суммы, оплаченной рублями
MAX_POINTS_PERCENT = _env_float("MAX_POINTS_PERCENT", 30)   # макс. доля суммы заказа, которую можно оплатить баллами

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_BADGES = {"hit", "trend", "sale", "instock"}
ALLOWED_PROMO_TYPES = {"percent", "fixed"}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 МБ на файл


# =====================================================================
#  ОТЗЫВЫ: база данных, API и модерация в Telegram
# =====================================================================

DATABASE_URL = os.environ.get("DATABASE_URL") or "sqlite:///" + os.path.join(APP_DIR, "shop.db")
if DATABASE_URL.startswith("postgres://"):          # старый формат ссылки
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL, pool_pre_ping=True, pool_recycle=300,
    json_serializer=lambda o: json.dumps(o, ensure_ascii=False),
)
SessionLocal = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))
Base = declarative_base()


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 5", name="ck_reviews_rating"),)

    id          = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    author_name = Column(String(64), nullable=False)
    rating      = Column(Integer, nullable=False)
    text        = Column(Text, nullable=False)
    is_approved = Column(Boolean, nullable=False, default=False, index=True)
    created_at  = Column(DateTime(timezone=True), nullable=False,
                         default=lambda: datetime.now(timezone.utc), index=True)

    def to_public(self):
        """Публичный вид: telegram_id наружу не отдаём."""
        created = self.created_at
        if created is not None and created.tzinfo is None:     # SQLite отдаёт без tz
            created = created.replace(tzinfo=timezone.utc)
        return {
            "id": self.id,
            "author_name": self.author_name,
            "rating": self.rating,
            "text": self.text,
            "created_at": created.isoformat() if created else None,
        }

    def to_admin(self):
        """Вид для админ-панели: с ID автора и статусом модерации."""
        d = self.to_public()
        d["telegram_id"] = self.telegram_id
        d["is_approved"] = bool(self.is_approved)
        return d


# ---------------------------------------------------------------------
#  ВСЕ ДАННЫЕ МАГАЗИНА — В БАЗЕ (Neon): товары, заказы, вопросы, промокоды, фото
# ---------------------------------------------------------------------
EXT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif"}
JSONType = JSON().with_variant(JSONB(), "postgresql")


class _DocMixin:
    """Документ с числовым id и JSON-телом — та же структура, что раньше лежала в *.json."""
    id   = Column(Integer, primary_key=True, autoincrement=False)
    data = Column(JSONType, nullable=False)


class Product(_DocMixin, Base):
    __tablename__ = "products"


class Order(_DocMixin, Base):
    __tablename__ = "orders"


class Question(_DocMixin, Base):
    __tablename__ = "questions"


class Promo(_DocMixin, Base):
    __tablename__ = "promos"


class StoredImage(Base):
    """Фото товаров хранятся прямо в базе (bytea) и отдаются по /uploads/<имя>."""
    __tablename__ = "images"
    filename     = Column(String(80), primary_key=True)
    content_type = Column(String(64), nullable=False)
    data         = Column(LargeBinary, nullable=False)
    created_at   = Column(DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(timezone.utc))


class Meta(Base):
    __tablename__ = "meta"
    key   = Column(String(64), primary_key=True)
    value = Column(Text)


class LoyaltyUser(Base):
    """Баланс баллов покупателя. 1 балл = 1 рубль."""
    __tablename__ = "loyalty_users"
    id             = Column(Integer, primary_key=True)
    telegram_id    = Column(BigInteger, nullable=False, unique=True, index=True)
    points_balance = Column(Integer, nullable=False, default=0)
    total_spent    = Column(Float, nullable=False, default=0)
    created_at     = Column(DateTime(timezone=True), nullable=False,
                            default=lambda: datetime.now(timezone.utc))


class LoyaltyLog(Base):
    """Журнал движения баллов: заказы (авто) и ручные корректировки из админки."""
    __tablename__ = "loyalty_log"
    id            = Column(Integer, primary_key=True)
    telegram_id   = Column(BigInteger, nullable=False, index=True)
    delta         = Column(Integer, nullable=False)           # + начислено / − списано
    balance_after = Column(Integer, nullable=False)
    kind          = Column(String(16), nullable=False, default="order")   # order | admin
    note          = Column(String(255))
    created_at    = Column(DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))


# Отдельная фабрика сессий для данных магазина (не мешает сессиям отзывов).
DocSession = sessionmaker(bind=engine, expire_on_commit=False)

try:
    Base.metadata.create_all(engine)       # создаст все таблицы, если их ещё нет
except Exception:
    log.exception("Не удалось подключиться к базе — проверьте DATABASE_URL")


def _get_or_create_loyalty(db, tg_id, lock=False):
    q = db.query(LoyaltyUser).filter(LoyaltyUser.telegram_id == tg_id)
    if lock:
        q = q.with_for_update()          # блокировка строки в PostgreSQL (в SQLite игнорируется)
    user = q.first()
    if user is None:
        user = LoyaltyUser(telegram_id=tg_id, points_balance=0, total_spent=0)
        db.add(user)
        try:
            db.flush()
        except IntegrityError:           # параллельный запрос успел создать запись
            db.rollback()
            user = q.first()
    return user


def loyalty_get(tg_id):
    """Баланс покупателя; если записи нет — создаёт с нулевым балансом."""
    with DocSession() as db:
        try:
            user = _get_or_create_loyalty(db, tg_id)
            db.commit()
            return {"telegram_id": user.telegram_id, "points_balance": user.points_balance}
        except Exception:
            db.rollback()
            raise


def _log_loyalty(db, tg_id, delta, balance_after, kind, note):
    if delta:
        db.add(LoyaltyLog(telegram_id=tg_id, delta=int(delta), balance_after=int(balance_after),
                          kind=kind, note=(note or "")[:255]))


def loyalty_apply(tg_id, spend, earn, paid, note=""):
    """Списывает spend баллов, начисляет earn баллов, добавляет paid к total_spent — одной транзакцией.
    Бросает ValueError, если на балансе не хватает баллов. Возвращает новый баланс."""
    with DocSession() as db:
        try:
            user = _get_or_create_loyalty(db, tg_id, lock=True)
            if spend > user.points_balance:
                raise ValueError("not enough points")
            after_spend = user.points_balance - spend
            user.points_balance = after_spend + earn
            user.total_spent = float(user.total_spent or 0) + paid
            _log_loyalty(db, tg_id, -spend, after_spend, "order", note)
            _log_loyalty(db, tg_id, earn, user.points_balance, "order", note)
            db.commit()
            return user.points_balance
        except Exception:
            db.rollback()
            raise


def loyalty_revert(tg_id, spend, earn, paid, note=""):
    """Обратная операция при отмене заказа: возвращает списанные баллы, забирает начисленный кешбэк."""
    with DocSession() as db:
        try:
            user = _get_or_create_loyalty(db, tg_id, lock=True)
            old = user.points_balance
            user.points_balance = max(0, old + spend - earn)
            user.total_spent = max(0.0, float(user.total_spent or 0) - paid)
            _log_loyalty(db, tg_id, user.points_balance - old, user.points_balance, "order", note)
            db.commit()
            return user.points_balance
        except Exception:
            db.rollback()
            raise


MAX_ADMIN_POINTS = 1_000_000


def loyalty_admin_adjust(tg_id, action, amount, note):
    """Ручная корректировка баланса из админки. action: add | subtract | set | reset.
    Баланс не уходит ниже нуля. Возвращает (старый_баланс, новый_баланс)."""
    with DocSession() as db:
        try:
            user = _get_or_create_loyalty(db, tg_id, lock=True)
            old = user.points_balance
            if action == "add":
                new = old + amount
            elif action == "subtract":
                new = max(0, old - amount)
            elif action == "set":
                new = amount
            else:  # reset
                new = 0
            user.points_balance = new
            _log_loyalty(db, tg_id, new - old, new, "admin", note)
            db.commit()
            return old, new
        except Exception:
            db.rollback()
            raise


def _load_docs(model):
    with DocSession() as db:
        rows = db.query(model).order_by(model.id).all()
        return [dict(r.data) for r in rows]


def _save_docs(model, items):
    """Синхронизирует таблицу со списком items: новые добавляет, изменённые обновляет,
    пропавшие удаляет. Неизменённые строки не трогаются."""
    new = {int(it["id"]): it for it in items}
    with DocSession() as db:
        try:
            existing = {r.id: r for r in db.query(model).all()}
            for rid, row in existing.items():
                if rid not in new:
                    db.delete(row)
            for rid, it in new.items():
                row = existing.get(rid)
                if row is None:
                    db.add(model(id=rid, data=it))
                elif row.data != it:
                    row.data = it
            db.commit()
        except Exception:
            db.rollback()
            raise


try:
    from PIL import Image, ImageOps
except Exception:          # Pillow не установлен — фото сохраним как есть
    Image = None


def prepare_image(raw, ext):
    """Сжимает фото (до 1600 px, WebP), чтобы база не пухла. Возвращает (bytes, content_type, ext)."""
    fallback_type = EXT_MIME.get(ext, "application/octet-stream")
    if Image is None:
        return raw, fallback_type, ext
    try:
        img = Image.open(io.BytesIO(raw))
        if getattr(img, "n_frames", 1) > 1:          # анимированный gif/webp — не трогаем
            return raw, fallback_type, ext
        img = ImageOps.exif_transpose(img)
        img.thumbnail((1600, 1600))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "transparency" in img.info or img.mode in ("P", "LA") else "RGB")
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=85, method=4)
        return buf.getvalue(), "image/webp", ".webp"
    except Exception:
        abort(400, description="Не удалось прочитать изображение")


def purge_unused_images(candidate_urls):
    """Удаляет из базы фото, на которые больше не ссылается ни один товар."""
    names = {u.rsplit("/", 1)[-1] for u in candidate_urls if isinstance(u, str) and u.startswith("/uploads/")}
    if not names:
        return
    still_used = set()
    for p in _load_docs(Product):
        for u in (p.get("images") or []) + [p.get("image")]:
            if isinstance(u, str):
                still_used.add(u.rsplit("/", 1)[-1])
    unused = names - still_used
    if not unused:
        return
    with DocSession() as db:
        try:
            db.query(StoredImage).filter(StoredImage.filename.in_(unused)).delete(synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("Не удалось удалить неиспользуемые фото")


def seed_from_files_once():
    """Разовый перенос: если в базе ещё пусто, берём products/orders/questions/promos из *.json
    и фото из папки uploads/. Повторно не запускается (флаг в таблице meta)."""
    try:
        with DocSession() as db:
            if db.get(Meta, "seeded_v1"):
                return
        for model, path in ((Product, PRODUCTS_FILE), (Order, ORDERS_FILE),
                            (Question, QUESTIONS_FILE), (Promo, PROMOS_FILE)):
            if os.path.exists(path) and not _load_docs(model):
                with open(path, "r", encoding="utf-8") as f:
                    items = json.load(f)
                if items:
                    _save_docs(model, items)
                    log.info("Перенесено в базу: %s (%d)", model.__tablename__, len(items))
        if os.path.isdir(UPLOAD_DIR):
            with DocSession() as db:
                for fn in os.listdir(UPLOAD_DIR):
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in ALLOWED_IMAGE_EXT and not db.get(StoredImage, fn):
                        with open(os.path.join(UPLOAD_DIR, fn), "rb") as f:
                            db.add(StoredImage(filename=fn, data=f.read(),
                                               content_type=EXT_MIME.get(ext, "application/octet-stream")))
                db.commit()
        with DocSession() as db:
            db.add(Meta(key="seeded_v1", value=datetime.now(timezone.utc).isoformat()))
            db.commit()
    except Exception:
        log.exception("Не удалось выполнить первичный перенос данных в базу")


@app.teardown_appcontext
def _close_db_session(exc=None):
    SessionLocal.remove()


REVIEW_MAX_TEXT = 2000
REVIEW_MAX_NAME = 64
REVIEW_MAX_PENDING = 3      # максимум неодобренных отзывов от одного пользователя


def review_keyboard(review_id):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"rv:ok:{review_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"rv:no:{review_id}"),
    )
    return kb


def notify_admins_new_review(review_id, author_name, username, telegram_id, rating, text):
    who = f"{author_name} (@{username})" if username else f"{author_name} (ID {telegram_id})"
    shown = text if len(text) <= 3000 else text[:3000] + "…"
    admin_text = (
        "💬 Новый отзыв на модерацию!\n"
        f"Автор: {who}\n"
        f"Оценка: {'⭐' * rating} ({rating}/5)\n"
        f"Текст: \"{shown}\""
    )
    kb = review_keyboard(review_id)
    for admin_id in ADMIN_CHAT_IDS:
        try:
            bot.send_message(admin_id, admin_text, reply_markup=kb)
        except Exception as e:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, e)


# --- API отзывов ------------------------------------------------------

@app.route("/api/reviews", methods=["GET"])
def get_reviews():
    db = SessionLocal()
    try:
        items = (
            db.query(Review)
            .filter(Review.is_approved.is_(True))
            .order_by(Review.created_at.desc(), Review.id.desc())
            .all()
        )
        count = len(items)
        average = round(sum(r.rating for r in items) / count, 1) if count else 0
        return jsonify({
            "reviews": [r.to_public() for r in items],
            "average": average,       # напр. 4.8 (из 5)
            "count": count,
        })
    except Exception:
        log.exception("Не удалось загрузить отзывы")
        return jsonify({"error": "Не удалось загрузить отзывы"}), 500


@app.route("/api/reviews", methods=["POST"])
def create_review():
    data = request.get_json(force=True, silent=True) or {}

    try:
        telegram_id = int(data.get("telegram_id"))
        if telegram_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        abort(400, description="Не удалось определить Telegram-аккаунт")

    author_name = " ".join(str(data.get("author_name") or "").split())[:REVIEW_MAX_NAME]
    text = str(data.get("text") or "").strip()
    username = re.sub(r"[^A-Za-z0-9_]", "", str(data.get("username") or "").lstrip("@"))[:64]
    if not author_name:
        abort(400, description="Не указано имя автора")
    if not text:
        abort(400, description="Напишите текст отзыва")
    if len(text) > REVIEW_MAX_TEXT:
        abort(400, description=f"Отзыв слишком длинный (максимум {REVIEW_MAX_TEXT} символов)")

    raw_rating = data.get("rating")
    try:
        if isinstance(raw_rating, bool):
            raise ValueError
        rating = int(raw_rating)
    except (TypeError, ValueError):
        abort(400, description="Поставьте оценку от 1 до 5")
    if not 1 <= rating <= 5:
        abort(400, description="Оценка должна быть от 1 до 5")

    db = SessionLocal()
    try:
        pending = (
            db.query(func.count(Review.id))
            .filter(Review.telegram_id == telegram_id, Review.is_approved.is_(False))
            .scalar()
        )
        if pending >= REVIEW_MAX_PENDING:
            return jsonify({"error": "Ваши прошлые отзывы ещё на модерации — подождите немного"}), 429

        review = Review(
            telegram_id=telegram_id,
            author_name=author_name,
            rating=rating,
            text=text,
            is_approved=False,          # виден только после одобрения админом
        )
        db.add(review)
        db.commit()
        review_id = review.id
    except Exception:
        db.rollback()
        log.exception("Не удалось сохранить отзыв")
        return jsonify({"error": "Не удалось сохранить отзыв"}), 500

    notify_admins_new_review(review_id, author_name, username, telegram_id, rating, text)
    return jsonify({"ok": True, "review_id": review_id, "status": "pending"}), 201


# --- Админ-панель: все отзывы и удаление --------------------------------

@app.route("/api/admin/reviews", methods=["GET"])
def admin_get_reviews():
    require_admin()
    db = SessionLocal()
    try:
        items = db.query(Review).order_by(Review.created_at.desc(), Review.id.desc()).all()
        return jsonify([r.to_admin() for r in items])
    except Exception:
        log.exception("Не удалось загрузить отзывы для админ-панели")
        return jsonify({"error": "Не удалось загрузить отзывы"}), 500


@app.route("/api/admin/reviews/<int:rid>", methods=["DELETE"])
def admin_delete_review(rid):
    require_admin()
    db = SessionLocal()
    try:
        review = db.get(Review, rid)
        if review is None:
            abort(404, description="Отзыв не найден")
        db.delete(review)
        db.commit()
    except Exception as e:
        db.rollback()
        if getattr(e, "code", None) == 404:
            raise
        log.exception("Не удалось удалить отзыв %s", rid)
        return jsonify({"error": "Не удалось удалить отзыв"}), 500
    return "", 204


# --- Модерация кнопками в Telegram -------------------------------------

REVIEW_APPROVED_TEXT = "✅ Отзыв одобрен и опубликован в магазине"
REVIEW_REJECTED_TEXT = "❌ Отзыв отклонен"
REVIEW_DONE_TEXT = "ℹ️ Этот отзыв уже обработан"


def _finish_moderation_message(call, new_text):
    """Заменяем текст сообщения админа (кнопки при этом исчезают)."""
    try:
        bot.edit_message_text(new_text, call.message.chat.id, call.message.message_id)
    except Exception as e:
        log.warning("Не удалось обновить сообщение модерации: %s", e)


@bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("rv:"))
def on_review_moderation(call: types.CallbackQuery):
    # кнопки доступны только администраторам
    if not call.from_user or call.from_user.id not in ADMIN_CHAT_IDS:
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return

    try:
        _, action, raw_id = call.data.split(":")
        review_id = int(raw_id)
        if action not in ("ok", "no"):
            raise ValueError
    except ValueError:
        bot.answer_callback_query(call.id, "Некорректные данные кнопки", show_alert=True)
        return

    db = SessionLocal()
    try:
        review = db.get(Review, review_id)

        # удалён или уже одобрен другим админом
        if review is None or review.is_approved:
            bot.answer_callback_query(call.id, "Этот отзыв уже обработан")
            _finish_moderation_message(call, REVIEW_DONE_TEXT)
            return

        if action == "ok":
            review.is_approved = True
            db.commit()
            result_text = REVIEW_APPROVED_TEXT
        else:
            db.delete(review)
            db.commit()
            result_text = REVIEW_REJECTED_TEXT
    except Exception:
        db.rollback()
        log.exception("Ошибка модерации отзыва %s", review_id)
        bot.answer_callback_query(call.id, "Ошибка базы данных, попробуйте ещё раз", show_alert=True)
        return
    finally:
        SessionLocal.remove()

    bot.answer_callback_query(call.id, "Готово")
    _finish_moderation_message(call, result_text)


# --- Вспомогательные функции для товаров и авторизации админа -----------

# Данные лежат в json-файлах, а gunicorn работает в нескольких потоках —
# блокировка не даёт двум запросам одновременно списать/перезаписать остатки.
DATA_LOCK = threading.RLock()


def locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with DATA_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def require_admin():
    if not ADMIN_PASSWORD:
        abort(403, description="ADMIN_PASSWORD не задан на сервере")
    supplied = request.headers.get("X-Admin-Password", "")
    if supplied != ADMIN_PASSWORD:
        abort(401, description="Неверный пароль")


def load_products():
    return _load_docs(Product)


def save_products(items):
    _save_docs(Product, items)


def load_orders():
    return _load_docs(Order)


def save_orders(items):
    _save_docs(Order, items)


def load_questions():
    return _load_docs(Question)


def save_questions(items):
    _save_docs(Question, items)


def load_promos():
    return _load_docs(Promo)


def save_promos(items):
    _save_docs(Promo, items)


seed_from_files_once()


def replace_catalog_once(flag="catalog_v2"):
    """Разовая замена каталога: все прежние товары удаляются, вместо них — товары из products.json.
    Заказы, баллы и промокоды не затрагиваются. Выполняется один раз (флаг в таблице meta)."""
    try:
        with DocSession() as db:
            if db.get(Meta, flag):
                return
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
            items = json.load(f)
        if not items:
            return
        old_urls = [u for p in _load_docs(Product) for u in (p.get("images") or []) + [p.get("image")]]
        _save_docs(Product, items)                      # лишние (старые) удаляются, новые добавляются
        purge_unused_images(old_urls)                   # фото старых товаров тоже чистим
        with DocSession() as db:
            db.add(Meta(key=flag, value=datetime.now(timezone.utc).isoformat()))
            db.commit()
        log.info("Каталог заменён: %d товаров", len(items))
    except Exception:
        log.exception("Не удалось заменить каталог")


replace_catalog_once()


def fmt_price(n):
    n = float(n)
    if n.is_integer():
        return f"{int(n):,}".replace(",", " ") + " ₽"
    return f"{n:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def sanitize_images(raw):
    if isinstance(raw, list):
        return [str(u).strip() for u in raw if isinstance(u, (str,)) and str(u).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def sanitize_badge(raw):
    b = str(raw or "").strip().lower()
    return b if b in ALLOWED_BADGES else None


def sanitize_stock(raw):
    """Остаток на складе: целое >= 0 либо None (None = учёт не ведётся, товар без ограничений)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return max(0, min(v, 1_000_000))


def qty_by_product(items):
    """Суммарное количество по id товара (один товар может быть в корзине с разными вкусами)."""
    totals = {}
    for i in items:
        try:
            pid = int(i.get("id"))
            qty = int(i.get("qty") or 1)
        except (TypeError, ValueError, AttributeError):
            continue
        totals[pid] = totals.get(pid, 0) + qty
    return totals


def check_stock(items, products_list=None, by_id=None):
    """Только проверяет, хватает ли остатков (ничего не списывает). Возвращает текст ошибки либо None."""
    totals = qty_by_product(items)
    if by_id is None:
        by_id = {p.get("id"): p for p in (products_list or load_products())}
    for pid, qty in totals.items():
        p = by_id.get(pid)
        if not p or p.get("stock") is None:
            continue
        have = int(p["stock"])
        if have <= 0:
            return f"«{p.get('name')}» — нет в наличии"
        if qty > have:
            return f"«{p.get('name')}» — доступно только {have} шт."
    return None


def reserve_stock(items):
    """Проверяет остатки и списывает их. Возвращает текст ошибки либо None. Вызывать под DATA_LOCK."""
    totals = qty_by_product(items)
    products_list = load_products()
    by_id = {p.get("id"): p for p in products_list}
    err = check_stock(items, by_id=by_id)
    if err:
        return err
    changed = False
    for pid, qty in totals.items():
        p = by_id.get(pid)
        if p and p.get("stock") is not None:
            p["stock"] = max(0, int(p["stock"]) - qty)
            changed = True
    if changed:
        save_products(products_list)
    return None


def shift_stock(items, sign):
    """Возвращает (sign=+1) или снова списывает (sign=-1, не ниже нуля) остатки по позициям заказа."""
    totals = qty_by_product(items)
    products_list = load_products()
    changed = False
    for p in products_list:
        qty = totals.get(p.get("id"))
        if qty and p.get("stock") is not None:
            p["stock"] = max(0, int(p["stock"]) + sign * qty)
            changed = True
    if changed:
        save_products(products_list)


def sanitize_old_price(raw):
    if raw in (None, ""):
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# --- Промокоды -----------------------------------------------------------

def sanitize_positive_int(raw):
    """Возвращает положительное целое либо None, если значение не задано/некорректно."""
    if raw in (None, ""):
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def find_promo_by_code(code):
    code_norm = (code or "").strip().upper()
    if not code_norm:
        return None
    promos = load_promos()
    return next((p for p in promos if p.get("code") == code_norm), None)


def validate_promo_for_use(promo, subtotal, customer_chat_id=None):
    """Проверяет, что промокод можно применить к заказу на сумму subtotal.
    Возвращает текст ошибки либо None, если всё в порядке."""
    if not promo:
        return "Промокод не найден"
    if not promo.get("active", True):
        return "Промокод больше не активен"
    max_uses = promo.get("max_uses")
    if max_uses is not None and int(promo.get("used_count") or 0) >= int(max_uses):
        return "Лимит использований промокода исчерпан"
    min_total = promo.get("min_total")
    if min_total is not None and subtotal < int(min_total):
        return f"Промокод действует от суммы заказа {fmt_price(min_total)}"
    if customer_chat_id and has_customer_used_promo(promo.get("code"), customer_chat_id):
        return "Вы уже использовали этот промокод"
    return None


def has_customer_used_promo(promo_code, customer_chat_id):
    """Проверяет, применял ли этот покупатель данный промокод ранее
    (отменённые заказы не считаются использованием)."""
    if not promo_code or not customer_chat_id:
        return False
    orders = load_orders()
    return any(
        o.get("promo_code") == promo_code
        and o.get("customer_chat_id") == customer_chat_id
        and o.get("status") not in CANCEL_STATUSES
        for o in orders
    )


def compute_promo_discount(promo, subtotal):
    if promo.get("type") == "percent":
        discount = subtotal * float(promo.get("value") or 0) / 100.0
    else:
        discount = float(promo.get("value") or 0)
    discount = max(0.0, min(discount, subtotal))
    return int(round(discount))


# --- Статусы заказа и их связь со складом/баллами ------------------------
# new / contacted  — «в обработке»: остатки НЕ списаны, кешбэк не начислен (баллы к оплате зарезервированы)
# accepted / done  — «принят»: остатки списаны, кешбэк начислен
# rejected / cancelled — «отклонён» / «отменён»: остатки не списаны, баллы возвращены
PENDING_STATUSES = {"new", "contacted"}
ACCEPTED_STATUSES = {"accepted", "done"}
CANCEL_STATUSES = {"rejected", "cancelled"}
ALL_STATUSES = PENDING_STATUSES | ACCEPTED_STATUSES | CANCEL_STATUSES


def _cb_credited(o):
    """Начислен ли кешбэк. У старых заказов флага нет: там кешбэк начислялся сразу вместе со списанием баллов."""
    return bool(o.get("cashback_credited", o.get("loyalty_applied")))


def apply_order_status(o, target):
    """Переводит заказ в статус target и приводит склад и баллы в соответствие.
    Возвращает текст ошибки (заказ при этом не меняется) либо None. Вызывать под DATA_LOCK;
    после успеха вызывающий сохраняет список заказов."""
    tg = o.get("customer_chat_id")
    items = o.get("items") or []
    spend = int(o.get("points_spent") or 0)
    cb = int(o.get("cashback") or 0)
    total = o.get("total") or 0

    have_cb = _cb_credited(o)
    o["cashback_credited"] = have_cb                       # зафиксировать для старых заказов
    have_spend = bool(o.get("loyalty_applied"))
    have_stock = bool(o.get("stock_deducted"))

    want_spend = target not in CANCEL_STATUSES
    want_hold = target in ACCEPTED_STATUSES

    # 1) сначала то, что может не получиться
    spend_acquired = False
    if want_spend and not have_spend and tg:
        try:
            loyalty_apply(tg, spend, 0, 0, f"Оплата баллами, заказ HQ-{o.get('id')}")
        except ValueError:
            return "У клиента не хватает баллов, чтобы возобновить заказ"
        spend_acquired = True
        o["loyalty_applied"] = True
    if want_hold and not have_stock:
        err = reserve_stock(items)
        if err:
            if spend_acquired:
                loyalty_revert(tg, spend, 0, 0, f"Возврат баллов, заказ HQ-{o.get('id')}")
                o["loyalty_applied"] = False
            return err
        o["stock_deducted"] = True

    # 2) затем освобождение
    if not want_hold and have_stock:
        shift_stock(items, +1)
        o["stock_deducted"] = False
    if tg:
        if want_hold and not have_cb:
            loyalty_apply(tg, 0, cb, total, f"Кешбэк за заказ HQ-{o.get('id')}")
            o["cashback_credited"] = True
        elif not want_hold and have_cb:
            loyalty_revert(tg, 0, cb, total, f"Отмена кешбэка, заказ HQ-{o.get('id')}")
            o["cashback_credited"] = False
        if not want_spend and o.get("loyalty_applied"):
            loyalty_revert(tg, spend, 0, 0, f"Возврат баллов, заказ HQ-{o.get('id')}")
            o["loyalty_applied"] = False
    o["status"] = target
    return None


ORDER_STATUS_TEXT = {
    "new": "в обработке", "contacted": "в обработке", "accepted": "принят", "done": "выполнен",
    "rejected": "отклонён", "cancelled": "отменён",
}


def order_keyboard(order_id):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Принять", callback_data=f"ord:ok:{order_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"ord:no:{order_id}"),
    )
    return kb


@bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("ord:"))
def on_order_moderation(call: types.CallbackQuery):
    if not call.from_user or call.from_user.id not in ADMIN_CHAT_IDS:
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return
    try:
        _, action, raw_id = call.data.split(":")
        oid = int(raw_id)
        if action not in ("ok", "no"):
            raise ValueError
    except ValueError:
        bot.answer_callback_query(call.id, "Некорректные данные кнопки", show_alert=True)
        return

    def finish(note):
        try:
            base = call.message.text or ""
            bot.edit_message_text(f"{base}\n\n{note}", call.message.chat.id, call.message.message_id)
        except Exception as e:
            log.warning("Не удалось обновить сообщение заказа: %s", e)

    with DATA_LOCK:
        orders = load_orders()
        order = next((o for o in orders if o.get("id") == oid), None)
        if order is None:
            bot.answer_callback_query(call.id, "Заказ не найден", show_alert=True)
            finish("ℹ️ Заказ не найден")
            return
        if order.get("status") not in PENDING_STATUSES:
            bot.answer_callback_query(call.id, "Этот заказ уже обработан")
            finish(f"ℹ️ Уже обработан: {ORDER_STATUS_TEXT.get(order.get('status'), order.get('status'))}")
            return
        try:
            err = apply_order_status(order, "accepted" if action == "ok" else "rejected")
            if not err:
                save_orders(orders)
        except Exception:
            log.exception("Ошибка обработки заказа %s", oid)
            bot.answer_callback_query(call.id, "Ошибка базы данных, попробуйте ещё раз", show_alert=True)
            return
        if err:
            bot.answer_callback_query(call.id, err, show_alert=True)   # напр. не хватает остатка — кнопки остаются
            return
        accepted = action == "ok"

    bot.answer_callback_query(call.id, "Готово")
    finish("✅ Заказ принят" if accepted else "❌ Заказ отклонён")
    chat_id = order.get("customer_chat_id")
    if chat_id:
        try:
            if accepted:
                cb = int(order.get("cashback") or 0)
                bot.send_message(chat_id, f"✅ Ваш заказ HQ-{oid} принят!\n\nМы свяжемся с вами в этом чате."
                                 + (f"\nНачислен кешбэк: {cb} б." if cb else ""))
            else:
                sp = int(order.get("points_spent") or 0)
                bot.send_message(chat_id, f"❌ К сожалению, заказ HQ-{oid} отклонён."
                                 + (f"\nСписанные баллы ({sp} б.) возвращены." if sp else "")
                                 + "\nЕсли остались вопросы — напишите менеджеру.")
        except Exception as e:
            log.warning("Не удалось уведомить покупателя о статусе заказа: %s", e)


# --- Telegram handlers -------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message):
    if not BASE_URL:
        bot.send_message(
            message.chat.id,
            "HQM SHOP: мини-апп ещё не сконфигурирован (не задан WEBHOOK_URL)."
        )
        return

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(
            text="🚀 Открыть HQM SHOP",
            web_app=types.WebAppInfo(url=BASE_URL + "/")
        )
    )

    if SHOP_CONTACT:
        keyboard.add(
            types.InlineKeyboardButton(
                text="💬 Написать менеджеру",
                url=f"https://t.me/{SHOP_CONTACT}"
            )
        )

    is_admin = bool(message.from_user) and message.from_user.id in ADMIN_CHAT_IDS
    if is_admin:
        keyboard.add(
            types.InlineKeyboardButton(
                text="Панель администратора",
                web_app=types.WebAppInfo(url=BASE_URL + "/admin")
            )
        )

    bot.send_message(
        message.chat.id,
        "🖤 *HQM SHOP*\n\n"
        "*Казань. Всё оригинал.*\n"
        "Выбирайте товары в каталоге.\n"
        f"🚚 Доставка — {DELIVERY_FEE} ₽" + (f", при покупке от {FREE_DELIVERY_FROM} ₽ — бесплатно" if FREE_DELIVERY_FROM > 0 else "") + "\n"
        "📦 Доставка по России — от Пятёрочки: получение на кассах или в постаматах\n\n"
        "Нажмите кнопку ниже, чтобы открыть каталог и оформить заказ.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )



@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message: types.Message):
    cmd_start(message)


# --- Flask routes --------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.route("/api/config", methods=["GET"])
def public_config():
    return jsonify({"contact": SHOP_CONTACT, "delivery_fee": DELIVERY_FEE,
                    "free_delivery_from": FREE_DELIVERY_FROM, "pickup_points": PICKUP_POINTS,
                    "cashback_percent": CASHBACK_PERCENT, "max_points_percent": MAX_POINTS_PERCENT,
                    "point_value": POINT_VALUE})


@app.route("/api/loyalty/balance", methods=["GET"])
def loyalty_balance():
    """Баланс баллов покупателя. Нет записи — создаём с балансом 0."""
    try:
        tg_id = int(request.args.get("telegram_id", ""))
        if tg_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        abort(400, description="Некорректный telegram_id")
    try:
        return jsonify(loyalty_get(tg_id))
    except Exception:
        log.exception("Не удалось получить баланс баллов")
        return jsonify({"error": "Не удалось получить баланс"}), 500


def parse_points(raw):
    """points_to_spend → целое число >= 0 (баллы списываются только целыми)."""
    if raw in (None, ""):
        return 0
    if isinstance(raw, bool):
        abort(400, description="Некорректное количество баллов")
    try:
        v = float(raw)
        if v < 0 or v != int(v):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        abort(400, description="Укажите количество баллов целым числом")
    return int(v)


def max_points_for(goods_total):
    """Максимум баллов к списанию: скидка в рублях (points * POINT_VALUE) не больше MAX_POINTS_PERCENT от суммы заказа."""
    return max(0, int(goods_total * MAX_POINTS_PERCENT / 100 / POINT_VALUE + 1e-9))


def cashback_points(paid_rub):
    """Кешбэк в баллах: round((сумма_рублями * 5%) / 0,5), округление «half up»."""
    return int(math.floor(paid_rub * CASHBACK_PERCENT / 100 / POINT_VALUE + 0.5 + 1e-9))


def order_loyalty_args(o):
    return (o.get("customer_chat_id"), int(o.get("points_spent") or 0),
            int(o.get("cashback") or 0), o.get("total") or 0)


@app.route("/products.json", methods=["GET"])
def products():
    # каталог берётся из базы; короткий кэш, чтобы не дёргать Neon на каждое открытие
    resp = jsonify(load_products())
    resp.headers["Cache-Control"] = "public, max-age=30"
    return resp


def parse_delivery(raw):
    """Проверяет блок доставки из заказа и возвращает (delivery, error)."""
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode")
    if mode not in ("pickup", "delivery"):
        return None, "Выберите самовывоз или доставку"

    d = {"mode": mode, "point": None, "address": None, "asap": True, "date": None, "time": None, "fee": 0}
    if mode == "pickup":
        point = (raw.get("point") or "").strip()
        if point not in PICKUP_POINTS:
            return None, "Выберите пункт самовывоза"
        d["point"] = point
    else:
        address = (raw.get("address") or "").strip()
        if len(address) < 5:
            return None, "Укажите адрес доставки"
        d["address"] = address[:300]
        d["fee"] = DELIVERY_FEE

    if not raw.get("asap", True):
        date_s = (raw.get("date") or "").strip()
        time_s = (raw.get("time") or "").strip()
        try:
            datetime.strptime(date_s, "%Y-%m-%d")
            datetime.strptime(time_s, "%H:%M")
        except ValueError:
            return None, "Укажите дату и время"
        d["asap"] = False
        d["date"] = date_s
        d["time"] = time_s
    return d, None


def delivery_text(d):
    if not isinstance(d, dict) or not d:
        return ""
    if d.get("mode") == "delivery":
        fee_txt = fmt_price(d["fee"]) if d.get("fee") else "бесплатно"
        line = f"🚚 Доставка ({fee_txt}): {d.get('address') or '—'}"
    else:
        line = f"📍 Самовывоз: {d.get('point') or '—'}"
    if d.get("asap", True):
        when = "⚡ Как можно скорее"
    else:
        try:
            day = datetime.strptime(d.get("date", ""), "%Y-%m-%d").strftime("%d.%m")
        except ValueError:
            day = d.get("date") or ""
        when = f"🕒 {day} в {d.get('time') or ''}"
    return line + "\n" + when


@app.route("/api/order", methods=["POST"])
@app.route("/api/orders", methods=["POST"])
@locked
def create_order():
    data = request.get_json(force=True, silent=True) or {}

    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    comment = (data.get("comment") or "").strip()
    items = data.get("items") or []

    if not name or not phone:
        abort(400, description="Укажите имя и телефон")
    if not isinstance(items, list) or not items:
        abort(400, description="Корзина пуста")
    for it in items:
        try:
            if not isinstance(it, dict) or int(it.get("qty") or 1) < 1:
                raise ValueError
        except (TypeError, ValueError):
            abort(400, description="Некорректное количество товара")

    delivery, delivery_err = parse_delivery(data.get("delivery"))
    if delivery_err:
        abort(400, description=delivery_err)

    subtotal = sum(int(i.get("price") or 0) * int(i.get("qty") or 1) for i in items)

    tg_user = data.get("tg_user") or {}
    try:
        customer_chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        customer_chat_id = None
    customer_username = (tg_user.get("username") or "").strip() or None

    promo_code_raw = (data.get("promo_code") or "").strip()
    promo = None
    discount = 0
    if promo_code_raw:
        promo = find_promo_by_code(promo_code_raw)
        promo_err = validate_promo_for_use(promo, subtotal, customer_chat_id)
        if promo_err:
            abort(400, description=promo_err)
        discount = compute_promo_discount(promo, subtotal)

    # --- баллы: проверяем до любых списаний ---
    goods_total = subtotal - discount                       # сумма корзины с учётом промокода
    if delivery["mode"] == "delivery":
        delivery["fee"] = delivery_fee_for(goods_total)     # бесплатно от FREE_DELIVERY_FROM
    points_to_spend = parse_points(data.get("points_to_spend"))
    if points_to_spend:
        if not customer_chat_id:
            abort(400, description="Оплата баллами доступна только при заказе из Telegram")
        try:
            balance = loyalty_get(customer_chat_id)["points_balance"]
        except Exception:
            log.exception("Не удалось прочитать баланс баллов")
            return jsonify({"error": "Не удалось проверить баланс баллов"}), 500
        if points_to_spend > balance:
            abort(400, description=f"Недостаточно баллов: на балансе {balance}")
        max_points = max_points_for(goods_total)
        if points_to_spend > max_points:
            abort(400, description=f"Баллами можно оплатить не больше {MAX_POINTS_PERCENT:g}% суммы заказа "
                                   f"(до {max_points} баллов)")

    points_discount = round(points_to_spend * POINT_VALUE, 2)      # скидка в рублях = баллы * 0,5
    total = round(goods_total - points_discount + delivery["fee"], 2)   # к оплате рублями
    cashback = cashback_points(total) if customer_chat_id else 0       # баллы за оплаченные рубли

    # остатки только проверяем: списание — после того, как админ нажмёт «Принять»
    stock_err = check_stock(items)
    if stock_err:
        abort(400, description=stock_err)

    new_balance = None
    if customer_chat_id:
        try:
            # баллы к оплате резервируем сразу, кешбэк начислится при принятии заказа
            new_balance = loyalty_apply(customer_chat_id, points_to_spend, 0, 0, "Оплата баллами нового заказа")
        except ValueError:
            abort(400, description="Недостаточно баллов на балансе")
        except Exception:
            log.exception("Не удалось обновить баланс баллов")
            return jsonify({"error": "Не удалось списать баллы, попробуйте ещё раз"}), 500

    orders = load_orders()
    new_id = (max([o.get("id", 0) for o in orders], default=0) + 1)
    order = {
        "id": new_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "phone": phone,
        "comment": comment,
        "items": items,
        "subtotal": subtotal,
        "discount": discount,
        "promo_code": promo.get("code") if promo else None,
        "delivery": delivery,
        "total": total,
        "customer_chat_id": customer_chat_id,
        "customer_username": customer_username,
        "status": "new",
        "stock_deducted": False,
        "cashback_credited": False,
        "goods_total": goods_total,
        "points_spent": points_to_spend,
        "points_discount": points_discount,
        "cashback": cashback,
        "loyalty_applied": bool(customer_chat_id),
        "messages": [],
    }
    orders.append(order)
    save_orders(orders)

    if promo:
        promos = load_promos()
        for p in promos:
            if p.get("id") == promo.get("id"):
                p["used_count"] = int(p.get("used_count") or 0) + 1
        save_promos(promos)

    items_lines = "\n".join(
        f"· {i.get('name','—')} ({i.get('size') or '—'}) × {i.get('qty', 1)}"
        for i in items
    )
    # для менеджера — с категорией и серией, т.к. названия вкусов повторяются в разных линейках
    _by_id = {p.get("id"): p for p in load_products()}
    def _where(i):
        try:
            p = _by_id.get(int(i.get("id")))
        except (TypeError, ValueError):
            p = None
        return " → ".join(x for x in (p.get("category"), p.get("subcategory")) if x) if p else ""
    admin_items_lines = "\n".join(
        f"· {i.get('name','—')} ({i.get('size') or '—'}) × {i.get('qty', 1)}"
        + (f"\n   📁 {_where(i)}" if _where(i) else "")
        for i in items
    )

    delivery_block = delivery_text(delivery)
    if delivery["mode"] == "delivery":
        fee_line = f"Доставка: {fmt_price(delivery['fee']) if delivery['fee'] else 'бесплатно'}\n"
    else:
        fee_line = ""
    loyalty_line = (f"Оплачено баллами: {points_to_spend} б. ({fmt_price(points_discount)}) | "
                    f"Кешбэк после подтверждения: {cashback} б. ({fmt_price(cashback * POINT_VALUE)})\n")

    discount_line = ""
    if discount > 0:
        discount_line = (
            f"Сумма товаров: {fmt_price(subtotal)}\n"
            f"Промокод: {promo.get('code')} (−{fmt_price(discount)})\n"
        )

    if customer_chat_id:
        try:
            bot.send_message(
                customer_chat_id,
                "✅ Заказ оформлен!\n\n"
                f"Номер заказа: HQ-{new_id}\n"
                f"{items_lines}\n\n"
                f"{delivery_block}\n\n"
                f"{discount_line}"
                f"{fee_line}"
                f"{loyalty_line}"
                f"Итого: {fmt_price(total)}\n\n"
                "Заказ в обработке. Как только менеджер его примет, мы сообщим вам в этом чате."
            )
        except Exception as e:
            log.warning("Не удалось отправить подтверждение покупателю: %s", e)

    admin_text = (
        f"🛒 Новый заказ HQ-{new_id}\n\n"
        f"Клиент: {name}\n"
        f"Телефон: {phone}\n"
        + (f"Telegram: @{customer_username}\n" if customer_username else "")
        + f"\n{admin_items_lines}\n\n"
        f"{delivery_block}\n\n"
        f"{discount_line}"
        f"{fee_line}"
        f"{loyalty_line}"
        f"Итого: {fmt_price(total)}\n"
        + (f"\nКомментарий: {comment}\n" if comment else "")
        + "\nТовар спишется со склада после нажатия «Принять»."
    )
    kb = order_keyboard(new_id)
    for admin_id in ADMIN_CHAT_IDS:
        try:
            bot.send_message(admin_id, admin_text, reply_markup=kb)
        except Exception as e:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, e)

    return jsonify({"ok": True, "order_id": new_id, "total": total, "points_spent": points_to_spend,
                    "points_discount": points_discount, "cashback": cashback, "points_balance": new_balance}), 201


@app.route("/api/promo/check", methods=["POST"])
def check_promo():
    """Проверка промокода из корзины (до оформления заказа)."""
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip()
    try:
        subtotal = int(float(data.get("subtotal") or 0))
    except (TypeError, ValueError):
        subtotal = 0
    tg_user = data.get("tg_user") or {}
    try:
        customer_chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        customer_chat_id = None
    if not code:
        abort(400, description="Введите промокод")

    promo = find_promo_by_code(code)
    err = validate_promo_for_use(promo, subtotal, customer_chat_id)
    if err:
        abort(400, description=err)

    discount = compute_promo_discount(promo, subtotal)
    return jsonify({
        "ok": True,
        "code": promo.get("code"),
        "type": promo.get("type"),
        "value": promo.get("value"),
        "discount": discount,
        "new_total": subtotal - discount,
    })


@app.route("/api/orders", methods=["GET"])
def customer_orders():
    """История заказов покупателя (по его Telegram ID)."""
    try:
        tg_id = int(request.args.get("tg_id", ""))
    except (TypeError, ValueError):
        abort(400, description="Некорректный tg_id")
    orders = load_orders()
    mine = [o for o in orders if o.get("customer_chat_id") == tg_id]
    mine.sort(key=lambda o: o.get("id", 0), reverse=True)
    return jsonify(mine)


@app.route("/api/orders/<int:oid>/cancel", methods=["POST"])
@locked
def customer_cancel_order(oid):
    """Покупатель отменяет свой заказ, если он ещё не взят в обработку."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        tg_id = int(data.get("tg_id"))
    except (TypeError, ValueError):
        abort(400, description="Некорректный tg_id")

    orders = load_orders()
    order = next((o for o in orders if o.get("id") == oid), None)
    if not order:
        abort(404, description="Заказ не найден")
    if order.get("customer_chat_id") != tg_id:
        abort(403, description="Это не ваш заказ")
    if order.get("status") != "new":
        abort(400, description="Заказ уже обработан — отмена недоступна")

    err = apply_order_status(order, "cancelled")   # вернёт баллы, товар на склад (если был списан)
    if err:
        abort(400, description=err)
    save_orders(orders)

    for admin_id in ADMIN_CHAT_IDS:
        try:
            bot.send_message(admin_id, f"❌ Клиент отменил заказ HQ-{oid}")
        except Exception as e:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, e)

    return jsonify(order)


@app.route("/api/question", methods=["POST"])
@locked
def create_question():
    data = request.get_json(force=True, silent=True) or {}

    question = (data.get("question") or "").strip()
    product_name = (data.get("product_name") or "").strip()
    try:
        product_id = int(data.get("product_id"))
    except (TypeError, ValueError):
        product_id = None

    if not question:
        abort(400, description="Введите вопрос")

    tg_user = data.get("tg_user") or {}
    try:
        customer_chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        customer_chat_id = None
    customer_username = (tg_user.get("username") or "").strip() or None

    questions = load_questions()
    new_id = (max([q.get("id", 0) for q in questions], default=0) + 1)
    entry = {
        "id": new_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "product_id": product_id,
        "product_name": product_name,
        "question": question,
        "customer_chat_id": customer_chat_id,
        "customer_username": customer_username,
        "status": "new",
        "messages": [],
    }
    questions.append(entry)
    save_questions(questions)

    if customer_chat_id:
        try:
            bot.send_message(
                customer_chat_id,
                "✅ Ваш вопрос отправлен администратору.\n\n"
                f"Товар: {product_name}\n"
                f"Вопрос: {question}\n\n"
                "Мы ответим вам в этом чате."
            )
        except Exception as e:
            log.warning("Не удалось отправить подтверждение по вопросу: %s", e)

    admin_text = (
        f"❓ Новый вопрос по товару «{product_name}»\n\n"
        + (f"От: @{customer_username}\n\n" if customer_username else "\n")
        + f"{question}\n\n"
        "Откройте панель администратора (вкладка «Вопросы»), чтобы ответить клиенту."
    )
    for admin_id in ADMIN_CHAT_IDS:
        try:
            bot.send_message(admin_id, admin_text)
        except Exception as e:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, e)

    return jsonify({"ok": True, "question_id": new_id}), 201


@app.route("/uploads/<path:filename>", methods=["GET"])
def uploaded_file(filename):
    # фото лежат в базе; имена — uuid, содержимое не меняется, кэшируем надолго
    with DocSession() as db:
        img = db.get(StoredImage, filename)
        if img is None:
            abort(404)
        resp = Response(img.data, mimetype=img.content_type)
    resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    resp.set_etag(filename)
    return resp.make_conditional(request)


@app.route("/admin", methods=["GET"])
def admin_page():
    return send_from_directory(APP_DIR, "admin.html")


@app.route("/brand/<path:filename>", methods=["GET"])
def brand_file(filename):
    resp = send_from_directory(os.path.join(APP_DIR, "brand"), filename)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/healthz", methods=["GET"])
def healthz():
    return {"status": "ok"}


# --- Admin API -------------------------------------------------------

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    require_admin()
    return jsonify({"ok": True})


@app.route("/api/admin/products", methods=["GET"])
def admin_get_products():
    require_admin()
    return jsonify(load_products())


@app.route("/api/admin/products", methods=["POST"])
@locked
def admin_create_product():
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    items = load_products()
    new_id = (max([p.get("id", 0) for p in items], default=0) + 1)
    sizes_raw = data.get("sizes", "")
    sizes = [s.strip() for s in sizes_raw.split(",") if s.strip()] if isinstance(sizes_raw, str) else (sizes_raw or [])
    try:
        price = int(float(data.get("price") or 0))
    except (TypeError, ValueError):
        price = 0
    images = sanitize_images(data.get("images") if "images" in data else data.get("image"))
    product = {
        "id": new_id,
        "name": (data.get("name") or "").strip(),
        "category": (data.get("category") or "").strip(),
        "subcategory": (data.get("subcategory") or "").strip(),
        "price": price,
        "old_price": sanitize_old_price(data.get("old_price")),
        "badge": sanitize_badge(data.get("badge")),
        "sizes": sizes or ["Стандарт"],
        "swatch": int(data.get("swatch") or 0) % 6,
        "desc": (data.get("desc") or "").strip(),
        "images": images,
        "image": images[0] if images else None,
        "stock": sanitize_stock(data.get("stock")),
    }
    items.append(product)
    save_products(items)
    return jsonify(product), 201


@app.route("/api/admin/products/<int:pid>", methods=["PUT"])
@locked
def admin_update_product(pid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    items = load_products()
    for p in items:
        if p.get("id") == pid:
            if "name" in data:
                p["name"] = (data.get("name") or "").strip()
            if "category" in data:
                p["category"] = (data.get("category") or "").strip()
            if "subcategory" in data:
                p["subcategory"] = (data.get("subcategory") or "").strip()
            if "price" in data:
                try:
                    p["price"] = int(float(data.get("price") or 0))
                except (TypeError, ValueError):
                    pass
            if "old_price" in data:
                p["old_price"] = sanitize_old_price(data.get("old_price"))
            if "badge" in data:
                p["badge"] = sanitize_badge(data.get("badge"))
            if "sizes" in data:
                sizes_raw = data.get("sizes", "")
                p["sizes"] = [s.strip() for s in sizes_raw.split(",") if s.strip()] if isinstance(sizes_raw, str) else (sizes_raw or p["sizes"])
            if "stock" in data:
                p["stock"] = sanitize_stock(data.get("stock"))
            if "desc" in data:
                p["desc"] = (data.get("desc") or "").strip()
            if "swatch" in data:
                p["swatch"] = int(data.get("swatch") or 0) % 6
            old_imgs = list(p.get("images") or [])
            if "images" in data:
                images = sanitize_images(data.get("images"))
                p["images"] = images
                p["image"] = images[0] if images else None
            elif "image" in data:
                images = sanitize_images(data.get("image"))
                p["images"] = images
                p["image"] = images[0] if images else None
            save_products(items)
            purge_unused_images(set(old_imgs) - set(p.get("images") or []))
            return jsonify(p)
    abort(404, description="Товар не найден")


@app.route("/api/admin/products/<int:pid>", methods=["DELETE"])
@locked
def admin_delete_product(pid):
    require_admin()
    items = load_products()
    new_items = [p for p in items if p.get("id") != pid]
    if len(new_items) == len(items):
        abort(404, description="Товар не найден")
    removed = next((p for p in items if p.get("id") == pid), {})
    save_products(new_items)
    purge_unused_images(removed.get("images") or [])
    return jsonify({"ok": True})


@app.route("/api/admin/upload", methods=["POST"])
def admin_upload():
    require_admin()
    file = request.files.get("file")
    if not file or not file.filename:
        abort(400, description="Файл не передан")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        abort(400, description="Недопустимый формат файла")
    data, content_type, ext = prepare_image(file.read(), ext)
    filename = f"{uuid.uuid4().hex}{ext}"
    with DocSession() as db:
        try:
            db.add(StoredImage(filename=filename, content_type=content_type, data=data))
            db.commit()
        except Exception:
            db.rollback()
            log.exception("Не удалось сохранить фото в базе")
            return jsonify({"error": "Не удалось сохранить фото"}), 500
    return jsonify({"url": f"/uploads/{filename}"}), 201


@app.route("/api/admin/promos", methods=["GET"])
def admin_get_promos():
    require_admin()
    promos = load_promos()
    promos.sort(key=lambda p: p.get("id", 0), reverse=True)
    return jsonify(promos)


@app.route("/api/admin/promos", methods=["POST"])
@locked
def admin_create_promo():
    require_admin()
    data = request.get_json(force=True, silent=True) or {}

    code = (data.get("code") or "").strip().upper()
    if not code:
        abort(400, description="Укажите код промокода")

    promo_type = (data.get("type") or "").strip().lower()
    if promo_type not in ALLOWED_PROMO_TYPES:
        abort(400, description="Некорректный тип скидки")

    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        abort(400, description="Укажите значение скидки")
    if value <= 0:
        abort(400, description="Значение скидки должно быть больше нуля")
    if promo_type == "percent" and value > 100:
        abort(400, description="Скидка в процентах не может быть больше 100")

    promos = load_promos()
    if any(p.get("code") == code for p in promos):
        abort(400, description="Такой промокод уже существует")

    new_id = (max([p.get("id", 0) for p in promos], default=0) + 1)
    promo = {
        "id": new_id,
        "code": code,
        "type": promo_type,
        "value": value,
        "active": bool(data.get("active", True)),
        "max_uses": sanitize_positive_int(data.get("max_uses")),
        "used_count": 0,
        "min_total": sanitize_positive_int(data.get("min_total")),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    promos.append(promo)
    save_promos(promos)
    return jsonify(promo), 201


@app.route("/api/admin/promos/<int:pid>", methods=["PUT"])
@locked
def admin_update_promo(pid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    promos = load_promos()
    for p in promos:
        if p.get("id") == pid:
            if "code" in data:
                code = (data.get("code") or "").strip().upper()
                if not code:
                    abort(400, description="Укажите код промокода")
                if any(o.get("code") == code and o.get("id") != pid for o in promos):
                    abort(400, description="Такой промокод уже существует")
                p["code"] = code
            if "type" in data:
                promo_type = (data.get("type") or "").strip().lower()
                if promo_type not in ALLOWED_PROMO_TYPES:
                    abort(400, description="Некорректный тип скидки")
                p["type"] = promo_type
            if "value" in data:
                try:
                    value = float(data.get("value"))
                except (TypeError, ValueError):
                    abort(400, description="Укажите значение скидки")
                if value <= 0:
                    abort(400, description="Значение скидки должно быть больше нуля")
                if p.get("type") == "percent" and value > 100:
                    abort(400, description="Скидка в процентах не может быть больше 100")
                p["value"] = value
            if "active" in data:
                p["active"] = bool(data.get("active"))
            if "max_uses" in data:
                p["max_uses"] = sanitize_positive_int(data.get("max_uses"))
            if "min_total" in data:
                p["min_total"] = sanitize_positive_int(data.get("min_total"))
            save_promos(promos)
            return jsonify(p)
    abort(404, description="Промокод не найден")


@app.route("/api/admin/promos/<int:pid>", methods=["DELETE"])
@locked
def admin_delete_promo(pid):
    require_admin()
    promos = load_promos()
    new_promos = [p for p in promos if p.get("id") != pid]
    if len(new_promos) == len(promos):
        abort(404, description="Промокод не найден")
    save_promos(new_promos)
    return jsonify({"ok": True})


@app.route("/api/admin/orders", methods=["GET"])
def admin_get_orders():
    require_admin()
    orders = load_orders()
    orders.sort(key=lambda o: o.get("id", 0), reverse=True)
    return jsonify(orders)


@app.route("/api/admin/orders/<int:oid>/status", methods=["PUT"])
@locked
def admin_update_order_status(oid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in ALL_STATUSES:
        abort(400, description="Некорректный статус")
    orders = load_orders()
    for o in orders:
        if o.get("id") == oid:
            err = apply_order_status(o, status)   # склад и баллы следуют за статусом
            if err:
                abort(400, description=err)
            save_orders(orders)
            return jsonify(o)
    abort(404, description="Заказ не найден")


@app.route("/api/admin/orders/<int:oid>/message", methods=["POST"])
@locked
def admin_message_customer(oid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        abort(400, description="Введите текст сообщения")

    orders = load_orders()
    order = next((o for o in orders if o.get("id") == oid), None)
    if not order:
        abort(404, description="Заказ не найден")

    chat_id = order.get("customer_chat_id")
    if not chat_id:
        abort(400, description="У этого заказа нет чата с покупателем в Telegram")

    try:
        bot.send_message(
            chat_id,
            f"💬 Сообщение от HQM SHOP по заказу HQ-{oid}:\n\n{text}"
        )
    except Exception as e:
        abort(400, description=f"Не удалось отправить сообщение: {e}")

    order.setdefault("messages", []).append({
        "text": text,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    if order.get("status") == "new":
        order["status"] = "contacted"
    save_orders(orders)
    return jsonify(order)


# --- Управление баллами покупателей (админка) -----------------------------

def _loyalty_customers():
    """Покупатели с балансом + данные из их заказов (имя, @username, резерв баллов)."""
    with DocSession() as db:
        rows = [(u.telegram_id, int(u.points_balance or 0), float(u.total_spent or 0), u.created_at)
                for u in db.query(LoyaltyUser).all()]
    info = {}
    for o in sorted(load_orders(), key=lambda x: x.get("id", 0)):
        tg = o.get("customer_chat_id")
        if not tg:
            continue
        d = info.setdefault(tg, {"orders": 0, "reserved": 0, "name": "", "username": "", "phone": "", "last_order": None})
        d["orders"] += 1
        d["name"] = o.get("name") or d["name"]
        d["username"] = o.get("customer_username") or d["username"]
        d["phone"] = o.get("phone") or d["phone"]
        d["last_order"] = o.get("created_at") or d["last_order"]
        if o.get("status") in PENDING_STATUSES and o.get("loyalty_applied"):
            d["reserved"] += int(o.get("points_spent") or 0)
    out = []
    for tg, bal, spent, created in rows:
        d = info.get(tg, {})
        if not bal and not d:        # открыл магазин, но баллов и заказов нет — не показываем
            continue
        out.append({
            "telegram_id": tg, "points_balance": bal, "total_spent": round(spent, 2),
            "name": d.get("name", ""), "username": d.get("username", ""), "phone": d.get("phone", ""),
            "orders": d.get("orders", 0), "reserved": d.get("reserved", 0),
            "last_order": d.get("last_order"),
        })
    out.sort(key=lambda c: (-c["points_balance"], -c["orders"]))
    return out


@app.route("/api/admin/loyalty", methods=["GET"])
def admin_get_loyalty():
    require_admin()
    customers = _loyalty_customers()
    return jsonify({
        "customers": customers,
        "point_value": POINT_VALUE,
        "total_points": sum(c["points_balance"] for c in customers),
        "with_points": sum(1 for c in customers if c["points_balance"] > 0),
    })


@app.route("/api/admin/loyalty/<int:tg_id>/history", methods=["GET"])
def admin_loyalty_history(tg_id):
    require_admin()
    with DocSession() as db:
        rows = (db.query(LoyaltyLog).filter(LoyaltyLog.telegram_id == tg_id)
                .order_by(LoyaltyLog.id.desc()).limit(60).all())
        return jsonify([{
            "id": r.id, "delta": r.delta, "balance_after": r.balance_after, "kind": r.kind,
            "note": r.note or "", "at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows])


@app.route("/api/admin/loyalty/<int:tg_id>/adjust", methods=["POST"])
def admin_loyalty_adjust(tg_id):
    """Начислить / списать / установить / обнулить баллы покупателя."""
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    action = (data.get("action") or "").strip()
    if action not in ("add", "subtract", "set", "reset"):
        abort(400, description="Некорректное действие")
    note = (data.get("note") or "").strip()[:200]

    amount = 0
    if action != "reset":
        try:
            amount = int(float(data.get("amount")))
        except (TypeError, ValueError):
            abort(400, description="Укажите количество баллов целым числом")
        if amount < 0 or (amount == 0 and action != "set"):
            abort(400, description="Количество баллов должно быть больше нуля")
        if amount > MAX_ADMIN_POINTS:
            abort(400, description="Слишком большое значение")

    try:
        old, new = loyalty_admin_adjust(tg_id, action, amount, note or "Корректировка администратором")
    except Exception:
        log.exception("Не удалось изменить баланс баллов")
        return jsonify({"error": "Не удалось изменить баланс, попробуйте ещё раз"}), 500

    delta = new - old
    notified = False
    if delta and data.get("notify"):
        if delta > 0:
            text = f"🎁 Вам начислено {delta} б. Баланс: {new} б."
        else:
            text = f"ℹ️ С вашего баланса списано {-delta} б. Баланс: {new} б."
        if note:
            text += f"\nПричина: {note}"
        try:
            bot.send_message(tg_id, text)
            notified = True
        except Exception as e:
            log.warning("Не удалось уведомить покупателя %s: %s", tg_id, e)

    return jsonify({"ok": True, "telegram_id": tg_id, "old_balance": old, "points_balance": new,
                    "delta": delta, "notified": notified})


@app.route("/api/admin/questions", methods=["GET"])
def admin_get_questions():
    require_admin()
    questions = load_questions()
    questions.sort(key=lambda q: q.get("id", 0), reverse=True)
    return jsonify(questions)


@app.route("/api/admin/questions/<int:qid>/message", methods=["POST"])
@locked
def admin_message_question(qid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        abort(400, description="Введите текст ответа")

    questions = load_questions()
    q = next((x for x in questions if x.get("id") == qid), None)
    if not q:
        abort(404, description="Вопрос не найден")

    chat_id = q.get("customer_chat_id")
    if not chat_id:
        abort(400, description="У этого вопроса нет чата с покупателем в Telegram")

    try:
        bot.send_message(
            chat_id,
            f"💬 Ответ от HQM SHOP по товару «{q.get('product_name','')}»:\n\n{text}"
        )
    except Exception as e:
        abort(400, description=f"Не удалось отправить ответ: {e}")

    q.setdefault("messages", []).append({
        "text": text,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    q["status"] = "answered"
    save_questions(questions)
    return jsonify(q)


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "OK", 200


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
def handle_api_errors(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": getattr(err, "description", str(err))}), err.code
    return err


def setup_webhook():
    if not BASE_URL:
        log.warning("WEBHOOK_URL / RENDER_EXTERNAL_URL не заданы — вебхук не установлен.")
        return
    url = f"{BASE_URL}/webhook/{BOT_TOKEN}"
    bot.remove_webhook()
    bot.set_webhook(url=url, allowed_updates=["message", "callback_query"])
    log.info("Webhook установлен: %s", url)


setup_webhook()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
