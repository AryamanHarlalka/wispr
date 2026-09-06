Run all tests:

    for t in tests/test_*.py; do PYTHONPATH=$PWD .venv/bin/python $t || break; done

Suites: static integrity (no undefined names), audio failure, instant
paste guards, paste safety, double-tap, and quality (rules never eat
words, the learner only learns real mishearings, incremental decoding
stays bounded, the paste target is never Wispr itself).

No pytest dependency on purpose: these must be runnable on a fresh
clone with only the runtime requirements installed.
