# app.py
import os
from pathlib import Path
from dotenv import load_dotenv   # <-- importa dotenv PRIMA degli altri moduli

# -----------------------------
# Carica variabili da .env
# -----------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")   # <-- qui il .env viene caricato subito

from flask import Flask, send_from_directory, jsonify, request
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from werkzeug.middleware.proxy_fix import ProxyFix

from src.routes.attestati import attestati_bp
from src.routes.auth import auth_bp

# Se stai usando auth.py con i callback:
try:
    from src.routes.auth import register_jwt_callbacks
except Exception:
    register_jwt_callbacks = None

STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

def get_env_list(var_name: str) -> list[str]:
    raw = os.getenv(var_name, "").strip()
    if not raw:
        return []
    return [v.strip() for v in raw.split(",") if v.strip()]

# -----------------------------
# App factory
# -----------------------------
def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR))

    # ---- Config (env con fallback dev) ----
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "CHANGE-ME-DEV")
    app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "CHANGE-ME-JWT-DEV")

    # Limite upload (MB)
    max_mb = int(os.getenv("MAX_UPLOAD_MB", "50"))
    app.config["MAX_CONTENT_LENGTH"] = max_mb * 1024 * 1024

    # JWT header-only
    app.config["JWT_TOKEN_LOCATION"] = ["headers"]
    app.config["JWT_HEADER_TYPE"] = "Bearer"

    # ---- Estensioni ----
    jwt = JWTManager(app)
    if register_jwt_callbacks:
        register_jwt_callbacks(jwt)

    # CORS
    cors_origins = get_env_list("CORS_ORIGINS")
    if cors_origins:
        CORS(app, resources={r"/api/*": {"origins": cors_origins}})
    else:
        CORS(app)  # fallback dev

    # Trust proxy headers
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    # ---- Blueprint ----
    app.register_blueprint(attestati_bp, url_prefix="/api/attestati")
    app.register_blueprint(auth_bp, url_prefix="/api/auth")

    # ---- Health check ----
    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"}), 200

    # ---- Error handlers JSON ----
    @app.errorhandler(413)
    def too_large(e):
        return jsonify({"error": f"File troppo grande (max {max_mb}MB)"}), 413

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found"}), 404
        return serve("")

    # ---- SPA + statici ----
    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve(path):
        static_folder_path = app.static_folder
        if not static_folder_path:
            return "Static folder not configured", 404

        file_path = os.path.join(static_folder_path, path)
        if path != "" and os.path.exists(file_path):
            resp = send_from_directory(static_folder_path, path)
            resp.cache_control.max_age = 3600
            return resp

        index_path = os.path.join(static_folder_path, "index.html")
        if os.path.exists(index_path):
            return send_from_directory(static_folder_path, "index.html")
        return "index.html not found", 404

    @app.route("/admin")
    def serve_admin():
        admin_path = os.path.join(app.static_folder, "admin.html")
        if os.path.exists(admin_path):
            return send_from_directory(app.static_folder, "admin.html")
        return "admin.html not found", 404

    return app

# -----------------------------
# Entrypoint dev
# -----------------------------
if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
