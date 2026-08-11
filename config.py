import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "измени-меня-в-проде")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'coop.db')}"
    )
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "instance", "uploads")
