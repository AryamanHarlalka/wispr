Run all tests:

    PYTHONPATH=\$PWD .venv/bin/python tests/test_audio_failure.py
    PYTHONPATH=\$PWD .venv/bin/python tests/test_instant.py

No pytest dependency on purpose: these must be runnable on a fresh
clone with only the runtime requirements installed.
