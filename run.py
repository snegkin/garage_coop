import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    # debug=True включает интерактивный отладчик Werkzeug (произвольное
    # выполнение кода через браузер) — категорически нельзя оставлять
    # включённым в проде. Управляется переменной окружения FLASK_DEBUG.
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")
