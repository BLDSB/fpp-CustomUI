from app import create_app

app = create_app()

if __name__ == "__main__":
    # Debug mode is controlled by FLASK_DEBUG env var.
    # On the Pi use: FLASK_DEBUG=0 python run.py
    app.run(host="0.0.0.0", port=5000)
