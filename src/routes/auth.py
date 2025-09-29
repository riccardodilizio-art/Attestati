# auth.py
import os
import logging
from datetime import timedelta, timezone, datetime
from flask import Blueprint, request, jsonify
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt,
    get_jwt_identity,
    jwt_required,
)
from werkzeug.security import check_password_hash, generate_password_hash

auth_bp = Blueprint("auth", __name__)

# =========================
#   CONFIGURAZIONE
# =========================

logger = logging.getLogger(__name__)

ADMIN_USER = os.getenv("ADMIN_USER")
ADMIN_PASS_HASH = os.getenv("ADMIN_PASS_HASH")

if not ADMIN_USER or not ADMIN_PASS_HASH:
    raise RuntimeError("⚠️ Configurazione mancante: devi impostare ADMIN_USER e ADMIN_PASS_HASH nelle variabili d'ambiente.")

ACCESS_EXPIRES = timedelta(minutes=int(os.getenv("JWT_ACCESS_MINUTES", "15")))
REFRESH_EXPIRES = timedelta(days=int(os.getenv("JWT_REFRESH_DAYS", "30")))

# Blocklist in memoria (demo) → sostituire con Redis/DB in produzione
TOKEN_BLOCKLIST = set()

def is_token_revoked(jti: str) -> bool:
    return jti in TOKEN_BLOCKLIST

# =========================
#   ROUTES
# =========================

@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"status": "error", "message": "Dati mancanti. Invia username e password."}), 400

    if username != ADMIN_USER or not check_password_hash(ADMIN_PASS_HASH, password):
        logger.warning(f"Tentativo login fallito per utente '{username}'")
        return jsonify({"status": "error", "message": "Credenziali errate"}), 401

    now = datetime.now(tz=timezone.utc)
    claims = {"role": "admin", "login_at": int(now.timestamp())}

    access_token = create_access_token(
        identity=username,
        additional_claims=claims,
        expires_delta=ACCESS_EXPIRES,
        fresh=True,
    )
    refresh_token = create_refresh_token(
        identity=username,
        additional_claims={"type": "refresh"},
        expires_delta=REFRESH_EXPIRES,
    )

    logger.info(f"Login eseguito con successo per utente '{username}'")

    return jsonify({
        "status": "success",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": int(ACCESS_EXPIRES.total_seconds()),
        "token_type": "Bearer"
    }), 200


@auth_bp.route("/refresh", methods=["POST"])
@jwt_required(refresh=True)
def refresh():
    identity = get_jwt_identity()
    now = datetime.now(tz=timezone.utc)
    claims = {"role": "admin", "refresh_at": int(now.timestamp())}

    new_access = create_access_token(
        identity=identity,
        additional_claims=claims,
        expires_delta=ACCESS_EXPIRES,
        fresh=False,
    )

    return jsonify({
        "status": "success",
        "access_token": new_access,
        "expires_in": int(ACCESS_EXPIRES.total_seconds()),
        "token_type": "Bearer"
    }), 200


@auth_bp.route("/logout", methods=["POST"])
@jwt_required()
def logout():
    jti = get_jwt().get("jti")
    if jti:
        TOKEN_BLOCKLIST.add(jti)
        logger.info(f"Token {jti} revocato con logout")
    return jsonify({"status": "success", "message": "Logout eseguito. Token revocato."}), 200

# =========================
#   CALLBACK UTILI
# =========================

def register_jwt_callbacks(jwt):
    @jwt.token_in_blocklist_loader
    def check_if_token_revoked(_jwt_header, jwt_payload: dict) -> bool:
        return is_token_revoked(jwt_payload.get("jti", ""))

    @jwt.expired_token_loader
    def expired_token_callback(jwt_header, jwt_payload):
        return jsonify({"status": "error", "message": "Token scaduto"}), 401

    @jwt.invalid_token_loader
    def invalid_token_callback(reason):
        return jsonify({"status": "error", "message": f"Token non valido: {reason}"}), 422

    @jwt.unauthorized_loader
    def missing_token_callback(reason):
        return jsonify({"status": "error", "message": f"Autenticazione richiesta: {reason}"}), 401

    @jwt.needs_fresh_token_loader
    def needs_fresh_token_callback(jwt_header, jwt_payload):
        return jsonify({"status": "error", "message": "Serve un token fresco per questa operazione"}), 401

    @jwt.revoked_token_loader
    def revoked_token_callback(jwt_header, jwt_payload):
        return jsonify({"status": "error", "message": "Token revocato"}), 401
