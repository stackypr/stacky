# Stacky Dev Notes

## Python Venv
Use the project venv at `~/.venv/stacky`.

Create it (one-time):
```bash
python3 -m venv ~/.venv/stacky
source ~/.venv/stacky/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements_lock.txt
python -m pip install -e .
```

Activate it (each shell):
```bash
source ~/.venv/stacky/bin/activate
```

## Tests
Run tests from the venv:
```bash
python -m pytest
```
