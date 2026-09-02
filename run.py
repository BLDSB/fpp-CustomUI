import logging

# Configure logging before the app is imported so every module's logger
# (alert monitor, startup sync, Flask) gets timestamps and INFO level.
# The systemd unit appends stdout/stderr to fpp-ui.log, so these lines are
# what a remote operator sees over Dataplicity.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from app import create_app

app = create_app()

if __name__ == "__main__":
    HOST, PORT = "0.0.0.0", 5000
    try:
        from waitress import serve
    except ImportError:
        # Debug mode is controlled by FLASK_DEBUG env var.
        # On the Pi use: FLASK_DEBUG=0 python run.py
        logging.getLogger(__name__).warning(
            "waitress is not installed — falling back to the Flask dev server. "
            "Run the plugin installer to fix dependencies."
        )
        app.run(host=HOST, port=PORT)
    else:
        logging.getLogger(__name__).info("Serving with waitress on %s:%s", HOST, PORT)
        serve(app, host=HOST, port=PORT, threads=8)
